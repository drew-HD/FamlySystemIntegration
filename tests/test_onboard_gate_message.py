"""Gate A message layout and the bill payer match question (same / siblings / different).

Fakes only. The forced create ("Different bill payer") is a Famly write, so its guards are tested end to end:
it sends ONE create (never addChildren), only when the fresh scan still shows the approved match, and the
read-back accepts only the new payer.
"""

import os
import tempfile
import unittest
from pathlib import Path

from actions.onboard import approval, bill_payer_writes as bpw, notifier, store, worker
from actions.onboard_dry_run import bill_payers as bp
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.registry import lookup as registry_lookup
from actions.registry import store as registry_store
from integrations import slack
from tests.test_onboard_bill_payer import CHILD, BPCase, BPFamly, payer
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID, FakeNotifier, FakeRest
from tests import fast_sqlite  # noqa: F401

OLDER = "older-child"


class QuestionNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.bp_gates = []

    def gate_bill_payer(self, text, job_id, plan_hash, nonce):
        self.bp_gates.append((text, job_id, plan_hash))
        self.gates.append((text, job_id, plan_hash))
        self.nonces.append(nonce)
        return f"C1:{len(self.gates)}"


class GateCase(BPCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("CHILDREN_REGISTRY_PATH")
        os.environ["CHILDREN_REGISTRY_PATH"] = str(Path(tmp.name) / "reg.sqlite3")
        self.addCleanup(lambda: os.environ.pop("CHILDREN_REGISTRY_PATH") if old is None
                        else os.environ.__setitem__("CHILDREN_REGISTRY_PATH", old))
        registry_store.upsert_child(OLDER, "Sam Quillfeather", "2021-05-02", "HDWEST", "operations")
        self.notify = QuestionNotifier()
        self.deps = worker.Deps(
            self.hub, self.famly, self.rest, self.notify,
            famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]), sleep=self.sleeps.append,
            registry=registry_lookup)

    def matched(self):
        self.famly.payers.append(payer(self.email, {OLDER: 1.0}, pid="the-one"))

    def click(self, action):
        job = store.get(self.job().id)
        return approval.handle_click(action, approval.button_value(job.id, job.plan_hash, job.approval_nonce), "U1")


class LayoutTests(GateCase):
    def test_a_new_child_message_reads_top_to_bottom(self):
        self.run_to_approval()
        text = self.notify.gates[0][0]
        order = ["Child: *Zara Quillfeather* (<", "Site: Westminster · Room: Littlewings · Start date: 15 January 2026",
                 "Child URN: already present (URN-1)", "*Actions*", "Child does not exist in Famly and will be created.",
                 "Contacts to be created:\nAda Lovelace (Ada.L@Example.org)\nBen Babbage (ben.b@example.org)",
                 "Ada Lovelace will be created and linked as the bill payer.", "*Summary*",
                 "• Child Zara Quillfeather to be created", "• Contact Ada Lovelace to be created",
                 "• Bill payer Ada Lovelace to be created and linked", "*Warnings*",
                 "Ben Babbage's address will not be added because the street, postcode or city is missing."]
        positions = [text.index(part) for part in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("HubSpot write", text)
        self.assertNotIn("Same bill payer", text)                 # no question without a match
        self.assertEqual(self.notify.bp_gates, [])                # plain Approve / Reject
        self.assert_gate_is_slack_safe()

    def test_names_never_reach_the_logs_or_the_jobs_table(self):
        self.matched()
        self.run_to_approval()
        self.click(slack.ACTION_ONBOARD_BP_SIBLINGS)
        self.run_worker()
        blob = "\n".join(self.logs + [store.all_rows_as_text()]).casefold()
        for value in PII + ["sam quillfeather"]:
            self.assertNotIn(value, blob, value)


class QuestionTests(GateCase):
    def test_a_match_asks_the_question_with_the_other_childs_name(self):
        self.matched()
        self.run_to_approval()
        self.assertEqual(len(self.notify.bp_gates), 1)
        text = self.notify.bp_gates[0][0]
        self.assertIn("Bill payer may already exist on Sam Quillfeather.", text)
        self.assertIn("• Bill payer Ada Lovelace may already exist, please confirm or deny", text)
        self.assertTrue(text.endswith(
            "Bill payer for *Zara Quillfeather* has the same email as the bill payer on *Sam Quillfeather*. "
            "Is this the same bill payer and are these children siblings?"))

    def test_each_button_approves_once_and_saves_its_choice(self):
        for action, choice in ((slack.ACTION_ONBOARD_BP_SAME, "same"), (slack.ACTION_ONBOARD_BP_SIBLINGS, "siblings"),
                               (slack.ACTION_ONBOARD_BP_DIFFERENT, "different"), (slack.ACTION_ONBOARD_APPROVE, None)):
            with self.subTest(action=action):
                store._reset_for_tests()
                self.famly = BPFamly()
                self.rest = FakeRest(self.famly)
                self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                        famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]),
                                        sleep=self.sleeps.append, registry=registry_lookup)
                self.matched()
                self.run_to_approval()
                stale = approval.button_value(self.job().id, self.job().plan_hash, "old-nonce")
                self.assertEqual(approval.handle_click(action, stale, "U1"), approval.MSG_STALE)
                self.assertEqual(self.click(action), approval.MSG_APPROVED)
                self.assertEqual(self.job().bill_payer_choice, choice)
                self.assertEqual(self.click(action), approval.MSG_HANDLED)     # a second press does nothing
                self.assertEqual(self.famly.bp_writes, [])                     # the click writes nothing

    def test_the_buttons_are_routed_as_onboarding_actions(self):
        for action in slack.ONBOARD_BP_ACTIONS:
            self.assertIn(action, slack.ONBOARD_ACTIONS)


class ChoiceWriteTests(GateCase):
    def run_choice(self, action):
        self.matched()
        self.run_to_approval()
        self.assertEqual(self.click(action), approval.MSG_APPROVED)
        return self.run_worker()

    def test_same_bill_payer_attaches_as_before(self):
        self.assertEqual(self.run_choice(slack.ACTION_ONBOARD_BP_SAME)["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])
        self.assertEqual(registry_store.siblings(CHILD), [])
        self.assertFalse([n for n in self.notify.notices if "Siblings" in n or "double check" in n])

    def test_siblings_attaches_records_the_pair_and_says_so(self):
        self.assertEqual(self.run_choice(slack.ACTION_ONBOARD_BP_SIBLINGS)["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])
        self.assertEqual(registry_store.siblings(CHILD), [OLDER])
        self.assertEqual(registry_store.siblings(OLDER), [CHILD])
        notice = next(n for n in self.notify.notices if n.startswith("👪"))
        self.assertIn("*Zara Quillfeather*", notice)
        self.assertIn("*Sam Quillfeather* share a bill payer. Saved in the children registry.", notice)

    def test_different_creates_one_new_payer_and_asks_for_a_double_check(self):
        self.assertEqual(self.run_choice(slack.ACTION_ONBOARD_BP_DIFFERENT)["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])                    # never addChildren
        old, new = self.famly.payers
        self.assertEqual(old["children"], {OLDER: 1.0})                        # the old payer untouched
        self.assertEqual(new["children"], {CHILD: 1.0})
        self.assertEqual(self.job().bill_payer_id, new["id"])
        notice = next(n for n in self.notify.notices if "double check" in n)
        self.assertIn("*Zara Quillfeather*", notice)
        self.assertIn("*Sam Quillfeather*", notice)
        self.assertIn("may cause issues with billing", notice)


class ForceCreateGuardTests(unittest.TestCase):
    """run_stage(force_create=True) directly, with a scripted scan."""

    def setUp(self):
        self.famly = BPFamly()
        from tests.test_onboard_bill_payer import deal_contact
        self.contacts = [deal_contact("ada@x.org")]
        self.approved = {"status": "would_attach", "flag": None, "billPayerId": "the-one", "matches": 1}

    def run_stage(self, scans, unresolved=False):
        script = iter(scans)
        return bpw.run_stage(public=self.public(), scan=lambda: next(script), contacts=self.contacts,
                             approved=self.approved, site_id=SITE_ID, child_id="kid", sleep=lambda s: None,
                             unresolved=unresolved, force_create=True)

    def public(self):
        return self.famly                 # the public GraphQL client fake (schema, create, addChildren)

    @staticmethod
    def p(pid, children=None):
        return bp.Payer(pid, "ada@x.org", dict(children or {}))

    def test_a_changed_match_writes_nothing(self):
        for scan in ([], [self.p("someone-else")], [self.p("the-one"), self.p("another")]):
            with self.subTest(scan=[x.id for x in scan]):
                result = self.run_stage([scan])
                self.assertEqual(self.famly.bp_writes, [])
                self.assertFalse(result.sent)

    def test_an_unconfirmed_earlier_write_is_never_repeated(self):
        result = self.run_stage([[self.p("the-one")]], unresolved=True)
        self.assertEqual((result.code, self.famly.bp_writes), (bp.CREATE_UNCONFIRMED, []))

    def test_the_read_back_accepts_only_the_one_new_payer(self):
        old = self.p("the-one", {"older": 1.0})
        ok = self.run_stage([[old], [old, self.p("bp-1", {"kid": 1.0})]])
        self.assertEqual((ok.code, ok.bill_payer_id), (bp.CREATED, "bp-1"))
        self.assertEqual([w[0] for w in self.famly.bp_writes], [bpw.CREATE_OP])

    def test_two_new_payers_or_the_child_on_the_old_payer_is_never_a_success(self):
        old = self.p("the-one", {"older": 1.0})
        for after in ([old, self.p("bp-1", {"kid": 1.0}), self.p("x", {"kid": 1.0})],
                      [self.p("the-one", {"older": 1.0, "kid": 1.0})]):
            with self.subTest(after=[x.id for x in after]):
                self.famly = BPFamly()
                result = self.run_stage([[old]] + [after] * 6)
                self.assertNotIn(result.code, bp.SUCCESS_CODES)
                self.assertEqual(len(self.famly.bp_writes), 1)                 # sent once, never again

    def test_without_force_the_same_match_attaches(self):
        self.famly.payers.append(payer("ada@x.org", {"older": 1.0}, pid="the-one"))
        old = self.p("the-one", {"older": 1.0})
        script = iter([[old], [self.p("the-one", {"older": 1.0, "kid": 1.0})]])
        result = bpw.run_stage(public=self.public(), scan=lambda: next(script), contacts=self.contacts,
                               approved=self.approved, site_id=SITE_ID, child_id="kid", sleep=lambda s: None)
        self.assertEqual(result.code, bp.ATTACHED)
        self.assertEqual([w[0] for w in self.famly.bp_writes], [bpw.ADD_CHILDREN_OP])


class RegistrySiblingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("CHILDREN_REGISTRY_PATH")
        os.environ["CHILDREN_REGISTRY_PATH"] = str(Path(tmp.name) / "reg.sqlite3")
        self.addCleanup(lambda: os.environ.pop("CHILDREN_REGISTRY_PATH") if old is None
                        else os.environ.__setitem__("CHILDREN_REGISTRY_PATH", old))

    def test_a_pair_is_one_row_in_either_order(self):
        self.assertEqual(registry_store.add_sibling("b", "a", "d1"), registry_store.ADDED)
        self.assertEqual(registry_store.add_sibling("a", "b", "d2"), registry_store.ALREADY)
        self.assertEqual(registry_lookup.record_siblings("a", ["b", "a"], "d3"), registry_store.ALREADY)
        self.assertEqual(registry_store.siblings("a"), ["b"])
        with self.assertRaises(ValueError):
            registry_store.add_sibling("a", "a", "d1")


if __name__ == "__main__":
    unittest.main()
