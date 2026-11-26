"""Registry stage 4: a nursery (operations) deal JOINS a child that ad hoc already created.

The planner rules (exact ad hoc only match joins; similar asks; a person's answer decides; the switch), the join
writer (dates: start only ever moved EARLIER, end date set; the Ad Hoc Only tag removed; every write settled by a
read and never re-sent), and the worker end to end with the real registry in a temp file. Fakes only."""

import os
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from actions.onboard import approval, join_writes, store
from actions.onboard_dry_run import runner as dry
from actions.registry import lookup as registry
from actions.registry import store as registry_store
from integrations import slack
from tests.test_onboard_phase2 import DEAL, SITE_ID, FakeNotifier, Phase2Case

import unittest

ADHOC_CHILD = "adhoc-child-1"
TAG = "tag-adhoc"


# --------------------------------------------------------------------------- #
# Planner rules (DryRun._registry_join), with a fake registry
# --------------------------------------------------------------------------- #
class FakeRegistry:
    def __init__(self, found=None, error=None):
        self.found = found or []
        self.error = error

    def operations_candidates(self, name, dob, deal_id):
        if self.error:
            raise self.error
        return self.found


class FakeExists:
    def __init__(self, exists=True):
        self.exists = exists

    def child_exists(self, child_id):
        return self.exists


def cand(child_id="k1", result="exact", adhoc_only=True, decision=None, urns=("hd0001cl",), site="HDWEST"):
    return {"childId": child_id, "result": result, "adhocOnly": adhoc_only, "decision": decision,
            "urns": list(urns), "siteCode": site}


DEAL_PROPS = {"child_date_of_birth": "2024-01-15", "child_first_name": "Zara", "child_last_name": "Quillfeather"}


class PlannerRuleTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ONBOARD_JOIN": "on"})
        env.start()
        self.addCleanup(env.stop)

    def join(self, found=None, exists=True, error=None):
        plan = {"holds": [], "warnings": []}
        runner = dry.DryRun(None, FakeExists(exists), registry=FakeRegistry(found, error))
        child, different = runner._registry_join(DEAL_PROPS, DEAL, plan)
        return child, different, plan["holds"]

    def test_one_exact_ad_hoc_only_match_joins_with_its_urn(self):
        child, _diff, holds = self.join([cand()])
        self.assertEqual(holds, [])
        self.assertEqual(child["status"], dry.CHILD_FOUND)
        self.assertEqual((child["famlyChildId"], child["via"]), ("k1", "registry"))
        self.assertEqual(child["join"], {"from": "adhoc", "decidedBy": "exact_match", "urn": "HD0001CL",
                                         "siteCode": "HDWEST"})

    def test_no_match_or_no_registry_creates_as_before(self):
        self.assertEqual(self.join([])[:2], (None, set()))
        plan = {"holds": [], "warnings": []}
        self.assertEqual(dry.DryRun(None, FakeExists())._registry_join(DEAL_PROPS, DEAL, plan), (None, set()))
        self.assertEqual(plan, {"holds": [], "warnings": []})

    def test_a_similar_match_asks_the_same_child_question(self):
        child, _diff, holds = self.join([cand(result="similar")])
        self.assertIsNone(child)
        self.assertEqual(holds, [{"reason": "registry_same_child_question", "childId": "k1", "childIds": ["k1"]}])

    def test_an_exact_match_that_is_not_ad_hoc_only_is_asked_not_joined(self):
        child, _diff, holds = self.join([cand(adhoc_only=False)])
        self.assertIsNone(child)
        self.assertEqual(holds[0]["reason"], "registry_same_child_question")

    def test_a_persons_answer_decides(self):
        child, _diff, holds = self.join([cand(result="similar", decision="same")])
        self.assertEqual((child["famlyChildId"], child["join"]["decidedBy"]), ("k1", "person"))
        child, different, holds = self.join([cand(result="similar", decision="different")])
        self.assertEqual((child, different, holds), (None, {"k1"}, []))
        # "same" for a child that is not ad hoc only (e.g. already has an operations deal): a person looks.
        child, _diff, holds = self.join([cand(adhoc_only=False, decision="same")])
        self.assertEqual(holds, [{"reason": "registry_child_needs_check", "childIds": ["k1"]}])
        child, _diff, holds = self.join([cand("k1", decision="same"), cand("k2", decision="same")])
        self.assertEqual(holds, [{"reason": "registry_child_needs_check", "childIds": ["k1", "k2"]}])

    def test_two_exact_matches_ask_about_one_at_a_time(self):
        child, _diff, holds = self.join([cand("k2"), cand("k1")])
        self.assertIsNone(child)
        self.assertEqual(holds, [{"reason": "registry_same_child_question", "childId": "k1",
                                  "childIds": ["k1", "k2"]}])

    def test_holds_unreadable_registry_missing_child_and_switch_off(self):
        self.assertEqual(self.join(error=OSError("x"))[2], [{"reason": "registry_unreadable"}])
        self.assertEqual(self.join([cand()], exists=False)[2],
                         [{"reason": "registry_child_not_in_famly", "childIds": ["k1"]}])
        os.environ["ONBOARD_JOIN"] = "off"
        child, _diff, holds = self.join([cand()])
        self.assertIsNone(child)
        self.assertEqual(holds, [{"reason": "registry_join_off", "childIds": ["k1"]}])


# --------------------------------------------------------------------------- #
# The join writer: dates and tag
# --------------------------------------------------------------------------- #
class FakeChild:
    """The child's summary, the dates mutation and the tag endpoints."""

    def __init__(self, start="2025-10-01", end=None, tags=(TAG,), apply=True):
        self.start, self.end, self.tags = start, end, set(tags)
        self.apply = apply              # does a write take effect?
        self.sent, self.posts = [], []
        self.raise_on_send = None
        self.read_error = None

    # internal GraphQL
    def execute(self, query_path, operation_name, variables):
        self.sent.append(variables)
        if self.apply:
            dates = variables["childActiveDates"]
            self.start = dates["startDate"]
            self.end = (dates.get("endInfo") or {}).get("endDate")
        if self.raise_on_send:
            raise self.raise_on_send
        return {"data": {}}

    # REST
    def get(self, path, params=None):
        if self.read_error:
            raise self.read_error
        if path == "/v2/tags":
            return [{"tagId": TAG, "title": "Ad Hoc Only", "institutionId": params["institutionId"]}]
        return {"child": {"localStartDate": self.start, "localEndDate": self.end},
                "tags": [{"tagId": t} for t in self.tags]}

    def post(self, path, json_body=None):
        self.posts.append((path, json_body))
        if self.apply:
            self.tags.discard(path.split("/")[3])
        return {}


class JoinWriterTests(unittest.TestCase):
    def writer(self, child, reason=None):
        return join_writes.JoinWriter(child, child, reason)

    def test_a_nursery_start_earlier_moves_the_start_and_sets_the_end(self):
        child = FakeChild(start="2026-02-01")
        codes = self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31))
        self.assertEqual(codes, ["join_start_moved", "join_end_set"])
        self.assertEqual(child.sent, [{"childId": "c1", "isParent": False, "childActiveDates": {
            "startDate": "2026-01-15", "endInfo": {"endDate": "2028-08-31"}}}])

    def test_a_nursery_start_later_never_moves_the_start_later(self):
        child = FakeChild(start="2025-10-01")
        codes = self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31))
        self.assertEqual(codes, ["join_start_kept", "join_end_set"])
        self.assertEqual(child.sent[0]["childActiveDates"]["startDate"], "2025-10-01")
        self.assertEqual(join_writes.start_to_send("2025-10-01", date(2026, 1, 15)), "2025-10-01")
        self.assertEqual(join_writes.start_to_send("2026-02-01", date(2026, 1, 15)), "2026-01-15")
        self.assertEqual(join_writes.start_to_send(None, date(2026, 1, 15)), "2026-01-15")

    def test_nothing_is_sent_when_the_dates_are_already_right(self):
        child = FakeChild(start="2025-10-01", end="2028-08-31")
        self.assertEqual(self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31)),
                         ["join_start_kept", "join_end_kept"])
        self.assertEqual(child.sent, [])

    def test_a_reason_for_leaving_is_sent_only_when_configured(self):
        child = FakeChild()
        self.writer(child, "reason-1").dates("c1", date(2026, 1, 15), date(2028, 8, 31))
        self.assertEqual(child.sent[0]["childActiveDates"]["endInfo"],
                         {"endDate": "2028-08-31", "reasonForLeavingId": "reason-1"})

    def test_an_unclear_send_is_settled_by_the_read_and_never_resent(self):
        child = FakeChild(start="2026-02-01")
        child.raise_on_send = TimeoutError()
        self.assertEqual(self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31)),
                         ["join_start_moved", "join_end_set"])
        self.assertEqual(len(child.sent), 1)

    def test_a_write_that_did_not_land_is_a_failure(self):
        child = FakeChild(start="2026-02-01", apply=False)
        self.assertEqual(self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31)),
                         ["join_dates_failed"])
        self.assertEqual(len(child.sent), 1)
        # The start landed but the end did not (e.g. Famly wants a reason for leaving).
        child = FakeChild(start="2025-10-01")
        original = child.execute
        def start_only(**kw):
            original(**kw)
            child.end = None
        child.execute = start_only
        self.assertEqual(self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31)),
                         ["join_start_kept", "join_end_date_failed"])

    def test_an_unreadable_child_sends_nothing(self):
        child = FakeChild()
        child.read_error = OSError("x")
        self.assertEqual(self.writer(child).dates("c1", date(2026, 1, 15), date(2028, 8, 31)),
                         ["join_summary_unreadable"])
        self.assertEqual(self.writer(child).untag("c1", [SITE_ID]), "join_tag_failed")
        self.assertEqual((child.sent, child.posts), ([], []))

    def test_untag_removes_the_tag_once_and_reads_it_back(self):
        child = FakeChild()
        self.assertEqual(self.writer(child).untag("c1", [SITE_ID]), "join_tag_removed")
        self.assertEqual(child.posts, [(f"/v2/tags/{TAG}/unassign", {"childId": "c1"})])
        self.assertEqual(self.writer(child).untag("c1", [SITE_ID]), "join_tag_absent")
        self.assertEqual(len(child.posts), 1)
        stuck = FakeChild(apply=False)
        self.assertEqual(self.writer(stuck).untag("c1", [SITE_ID]), "join_tag_failed")
        self.assertEqual(len(stuck.posts), 1)
        self.assertEqual(self.writer(FakeChild()).untag("c1", []), "join_tag_not_found")

    def test_the_dates_document_only_edits_active_dates(self):
        join_writes.assert_document()
        text = join_writes.DATES_QUERY.read_text(encoding="utf-8").lower()
        self.assertNotIn("group", "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))


# --------------------------------------------------------------------------- #
# The worker end to end, with the real registry in a temp file
# --------------------------------------------------------------------------- #
class QuestionNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.questions = []

    def same_child(self, text, job_id, child_id, nonce):
        self.questions.append((text, job_id, child_id, nonce))
        return "C1:q"


class FakeJoinWriter:
    def __init__(self):
        self.calls = []
        self.dates_codes = ["join_start_kept", "join_end_set"]
        self.tag_code = "join_tag_removed"

    def dates(self, child_id, start, end):
        self.calls.append(("dates", child_id, start, end))
        return list(self.dates_codes)

    def untag(self, child_id, institutions):
        self.calls.append(("untag", child_id, list(institutions)))
        return self.tag_code


class WorkerJoinTests(Phase2Case):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CHILDREN_REGISTRY_PATH": str(Path(tmp.name) / "reg.sqlite3"),
                                           "ONBOARD_JOIN": "on"})
        env.start()
        self.addCleanup(env.stop)
        self.notify = QuestionNotifier()
        self.joiner = FakeJoinWriter()
        self.deps.notifier, self.deps.registry, self.deps.join_writer = self.notify, registry, self.joiner
        self.hub.deals[DEAL]["child_urn"] = ""             # no URN on the nursery deal: only the registry finds it
        # The ad hoc child in Famly (no deal reference) and in the registry (an ad hoc deal, its URN).
        self.famly.children[ADHOC_CHILD] = {
            "name": "x", "externalId": "HD0009CL", "currentGroupId": "g",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2025-10-01"}], "externalSystemsReferences": []}
        self.famly.contacts[ADHOC_CHILD] = []              # its parents are added by the join (none in the fake)
        registry_store.upsert_child(ADHOC_CHILD, "Zara Quillfeather", "2024-01-15", "HDWEST", "adhoc")
        registry_store.add_urn(ADHOC_CHILD, "HD0009CL")
        registry_store.add_deal(ADHOC_CHILD, "adhoc-deal-7", registry_store.ADHOC)

    def test_an_exact_match_joins_after_gate_a_and_creates_no_child(self):
        self.run_to_approval()
        gate = self.notify.gates[-1][0]
        self.assertIn("join the ad hoc child", gate)
        self.assertIn(ADHOC_CHILD, gate)
        self.assertNotIn("Zara", gate)                     # no name in Gate A
        self.approve()
        result = self.run_worker()
        self.assertEqual(self.famly.child_creates, [])
        self.assertEqual(self.joiner.calls, [("dates", ADHOC_CHILD, date(2026, 1, 15), date(2028, 8, 31)),
                                             ("untag", ADHOC_CHILD, [SITE_ID])])
        self.assertEqual(self.written()["famly_childid"], ADHOC_CHILD)
        self.assertEqual(self.written()["child_urn"], "HD0009CL")
        self.assertEqual(self.written()["end_date"], "2028-08-31")       # the defaulted end date, deal was empty
        self.assertEqual(registry_store.by_deal(DEAL).famly_child_id, ADHOC_CHILD)
        summary = self.notify.notices[-1]
        self.assertIn("Zara Quillfeather", summary)                     # the join summary exception
        self.assertIn(ADHOC_CHILD, summary)
        self.assertNotIn("2024-01-15", summary)
        self.assertEqual(result["status"], store.DONE, result)
        self.assertFalse(any("Zara" in line or "Quillfeather" in line for line in self.logs))

    def test_a_join_write_that_failed_ends_needs_review_and_still_links_the_deal(self):
        self.joiner.dates_codes = ["join_dates_failed"]
        result = self.run_through()
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertIn("join_writes", store.get(result["jobId"]).last_error_code)
        self.assertEqual(self.famly.child_creates, [])
        self.assertEqual(self.written()["famly_childid"], ADHOC_CHILD)
        self.assertNotIn("end_date", self.written())                    # the end date did not land on the child
        self.assertIn("NOT confirmed", self.notify.notices[-1])

    def test_a_similar_name_asks_and_the_answer_runs_the_deal_again(self):
        registry_store.upsert_child(ADHOC_CHILD, "Zara Quilfeather", "2024-01-15", "HDWEST", "adhoc")
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.HELD)
        self.assertEqual(self.notify.gates, [])
        text, job_id, child_id, nonce = self.notify.questions[-1]
        self.assertEqual(child_id, ADHOC_CHILD)
        self.assertIn("Zara Quilfeather", text)
        self.assertNotIn("2024-01-15", text)
        value = approval.same_child_value(job_id, child_id, nonce)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_SAME_CHILD, value, "U1"), approval.MSG_SAME_YES)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_NEW_CHILD, value, "U1"),
                         approval.MSG_SAME_HANDLED)                      # a second press does nothing
        self.assertEqual(registry_store.decisions_for_deal(DEAL), {ADHOC_CHILD: "same"})
        result = self.run_worker()                                      # the fresh job: a join, at Gate A
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertIn("confirmed as the same child by a person", self.notify.gates[-1][0])

    def test_different_child_plans_a_create(self):
        registry_store.upsert_child(ADHOC_CHILD, "Zara Quilfeather", "2024-01-15", "HDWEST", "adhoc")
        self.enqueue()
        self.run_worker()
        _text, job_id, child_id, nonce = self.notify.questions[-1]
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_NEW_CHILD,
                                               approval.same_child_value(job_id, child_id, "wrong"), "U1"),
                         approval.MSG_SAME_STALE)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_NEW_CHILD,
                                               approval.same_child_value(job_id, child_id, nonce), "U1"),
                         approval.MSG_SAME_NO)
        self.run_worker()
        self.assertIn("create the child", self.notify.gates[-1][0])
        self.approve()
        self.run_worker()
        self.assertEqual(len(self.famly.child_creates), 1)
        new_child = registry_store.by_deal(DEAL)
        self.assertNotEqual(new_child.famly_child_id, ADHOC_CHILD)       # the new child's own registry row
        self.assertIn((DEAL, "operations"), new_child.deals)

    def test_join_switched_off_holds_and_sends_nothing(self):
        os.environ["ONBOARD_JOIN"] = "off"
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.HELD)
        self.assertIn("registry_join_off", store.get(result["jobId"]).last_error_code)
        self.assertEqual((self.famly.child_creates, self.joiner.calls), ([], []))


if __name__ == "__main__":
    unittest.main()
