"""Children registry in the ad hoc pipeline (registry stages 2 and 3): the planner's registry match, a child with an
operations deal, and the live writes and reports (registry rows, Slack, HubSpot note and follow-up). Fakes and a
temporary registry only."""

import logging
import os
import tempfile
import unittest
from unittest import mock

from actions.adhoc import approval, store, worker
from actions.adhoc_plan import planner
from actions.onboard_audit.runner import Index
from actions.registry import lookup, store as registry
from integrations import slack
from tests.test_adhoc_new_child import NewChildCase, URN, new_plan
from tests.test_adhoc_plan import LOOKUP, TODAY, Contact, deal, kid
from tests.test_adhoc_worker import Case, FakeWrites, plan

DOB = "2024-06-01"


class TempRegistry(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.reg_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"CHILDREN_REGISTRY_PATH": os.path.join(self.reg_dir.name, "r.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.reg_dir.cleanup)

    def add(self, child_id, name="Zed Quill", dob=DOB, deals=()):
        registry.upsert_child(child_id, name, dob, "HDCW", "test")
        for deal_id, kind in deals:
            registry.add_deal(child_id, deal_id, kind)


class PlannerTests(TempRegistry):
    def plan(self, children, contacts=(), plans=False, registry_fn=lookup.candidates, deals_fn=lookup.operations_deals,
             **over):
        deps = planner.Deps(has_plan=lambda cid: plans, read_sessions=lambda c, d: LOOKUP, preview=lambda c, p, d: {},
                            price_child_for_site=lambda s: "t", registry=registry_fn, registry_deals=deals_fn)
        confirmed = over.pop("confirmed", None)
        force_new = over.pop("force_new", False)
        return planner.build_plan(deal(**over), "Approved", list(contacts), Index({c.id: c for c in children}), deps,
                                  TODAY, confirmed_child_id=confirmed, force_new=force_new)

    def test_an_exact_registry_match_is_that_child_and_wins_over_the_email_hold(self):
        self.add("r1")
        sibling = kid("sib", name="Ada Quill", birthday="2022-02-02")          # the parent's email is on a sibling
        p = self.plan([kid("r1", emails=()), sibling], contacts=[Contact("mum@x.org")])
        self.assertEqual((p["match"]["via"], p["match"]["childIds"], p["holds"]), ("registry_exact", ["r1"], []))
        self.assertIn("registry_add_adhoc_deal", p["steps"])

    def test_a_middle_name_on_the_deal_is_still_exact(self):
        self.add("r1")
        p = self.plan([kid("r1", emails=())], child_full_name="Zed Mae Quill")
        self.assertEqual(p["match"]["childIds"], ["r1"])

    def test_two_exact_matches_are_held_ambiguous(self):
        self.add("r1"), self.add("r2")
        p = self.plan([kid("r1", emails=()), kid("r2", emails=())])
        self.assertIn("child_ambiguous", p["holds"])

    def test_a_similar_match_is_held_for_the_question_and_both_answers_work(self):
        self.add("r1", name="Zed Arlo Quill")
        children = [kid("r1", name="Zed Arlo Quill", emails=())]
        p = self.plan(children, child_full_name="Zed Bo Quill")
        self.assertEqual((p["holds"], p["match"]["candidateIds"], p["match"]["reason"]),
                         (["registry_similar_child"], ["r1"], "registry_similar"))
        new = self.plan(children, child_full_name="Zed Bo Quill", force_new=True)
        self.assertEqual((new["holds"], new["match"]["childIds"]), ([], []))
        self.assertIn("registry_add_child", new["steps"])
        same = self.plan(children, child_full_name="Zed Bo Quill", confirmed="r1")
        self.assertEqual((same["holds"], same["match"]["childIds"]), ([], ["r1"]))

    def test_twins_are_asked_a_first_name_alone_or_another_dob_is_a_new_child(self):
        self.add("r1", name="Ada Quill")
        self.assertIn("registry_similar_child", self.plan([kid("r1", name="Ada Quill", emails=())])["holds"])  # twin
        registry.upsert_child("r1", "Zed Other", DOB, "HDCW", "test")
        p = self.plan([kid("r1", name="Zed Other", emails=())])
        self.assertEqual((p["holds"], p["match"]["childIds"]), ([], []))
        registry.upsert_child("r1", "Zed Quill", "2024-06-02", "HDCW", "test")
        self.assertEqual(self.plan([kid("r1", birthday="2024-06-02", emails=())])["match"]["childIds"], [])

    def test_a_registry_child_not_in_the_live_roster_is_ignored(self):
        self.add("gone")
        p = self.plan([])
        self.assertEqual((p["match"]["childIds"], p["holds"]), ([], []))

    def test_an_unreadable_registry_holds_the_job(self):
        def boom(*a):
            raise OSError("disk")
        p = self.plan([kid("r1", emails=())], registry_fn=boom)
        self.assertEqual((p["holds"], p["match"]["childIds"]), (["registry_unreadable"], []))
        p = self.plan([kid("r1", emails=())], deals_fn=boom, famly_childid="r1")
        self.assertIn("registry_unreadable", p["holds"])

    def test_an_operations_child_is_never_ad_hoc_only_and_a_missing_plan_is_flagged(self):
        self.add("r1", deals=[("900", registry.OPERATIONS), ("901", registry.ADHOC)])
        p = self.plan([kid("r1", emails=())])
        self.assertEqual((p["adHocOnly"], p["match"]["operationsDealIds"]), (False, ["900"]))
        self.assertIn("operations_child_no_famly_plan", p["flags"])
        self.assertNotIn("assign_ad_hoc_only_tag", p["steps"])
        self.assertNotIn("remove_end_date_if_set", p["steps"])
        with_plan = self.plan([kid("r1", emails=())], plans=True)
        self.assertNotIn("operations_child_no_famly_plan", with_plan["flags"])
        self.assertIn("joins_operations_child", with_plan["flags"])


class FakeHubSpot:
    """deal_writer stand-in with notes and the follow-up dropdown."""

    def __init__(self, note="note_added", followup="followup_written"):
        self.calls, self._note, self._followup = [], note, followup

    def add_note(self, deal_id, body):
        self.calls.append(("note", deal_id, body))
        return self._note

    def set_followup(self, deal_id, value):
        self.calls.append(("followup", deal_id, value))
        return self._followup


def ops_plan(no_plan=True):
    p = dict(plan(), adHocOnly=False)
    p["match"] = {"via": "registry_exact", "childIds": ["c1"], "operationsDealIds": ["900"]}
    p["flags"] = ["joins_operations_child"] + (["operations_child_no_famly_plan"] if no_plan else [])
    return p


class LiveTests(TempRegistry, Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ADHOC_MODE": "live"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_live(self, p, hubspot=None, details=None, registry_mod=lookup):
        self.writes, self.hubspot = FakeWrites(), hubspot or FakeHubSpot()
        store.enqueue("1", "live")
        deps = self.deps(p)
        deps.writes, deps.deal_writer, deps.registry = self.writes, self.hubspot, registry_mod
        deps.child_details = lambda cid: details
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        return store.latest_for_deal("1")

    def test_an_existing_child_gets_its_registry_row_and_the_ad_hoc_deal(self):
        job = self.run_live(plan(), details={"name": "Zed Quill", "dob": DOB, "urn": "HD0001CW"})
        child = registry.by_child_id("c1")
        self.assertEqual((child.full_name, child.dob, child.urns, child.deals),
                         ("Zed Quill", DOB, ["hd0001cw"], [("1", registry.ADHOC)]))
        self.assertEqual(job.status, store.DONE)
        self.assertIn("registry:added", job.outcomes)
        self.assertIn("Children registry: updated.", self.notifier.sent[-1]["text"])
        self.assertEqual(self.hubspot.calls, [])                    # nothing to follow up: no HubSpot write

    def test_an_operations_child_is_booked_without_tag_and_reported_in_slack_and_hubspot(self):
        self.add("c1", deals=[("900", registry.OPERATIONS)])
        job = self.run_live(ops_plan())
        self.assertEqual([c[0] for c in self.writes.calls], ["book"])          # no tag, no end-date change
        self.assertEqual(job.status, store.DONE)
        text = self.notifier.sent[-1]["text"]
        self.assertIn("book on Famly next time", text)
        self.assertIn("Zed Quill (Famly Id: c1) is marked as having an operations deal", text)
        self.assertEqual([(k, d) for k, d, _ in self.hubspot.calls], [("note", "1"), ("note", "900"), ("followup", "900")])
        self.assertEqual(self.hubspot.calls[-1][2], "no_famly_plan")
        for _k, _d, body in self.hubspot.calls[:2]:
            self.assertNotIn("Zed", body)                                     # notes carry ids only

    def test_hubspot_failures_are_flagged_in_slack_and_never_stop_the_job(self):
        self.add("c1", deals=[("900", registry.OPERATIONS)])
        job = self.run_live(ops_plan(), hubspot=FakeHubSpot(note="note_failed", followup="followup_failed"))
        self.assertEqual(job.status, store.DONE)
        self.assertIn("Needs checking: HubSpot could not be updated", self.notifier.sent[-1]["text"])

    def test_a_registry_conflict_or_failure_is_flagged_never_a_stop(self):
        self.add("other", deals=[("1", registry.ADHOC)])                        # the deal is on another child
        job = self.run_live(plan(), details={"name": "Zed Quill", "dob": DOB, "urn": ""})
        self.assertEqual((job.status, registry.by_deal("1").famly_child_id), (store.DONE, "other"))
        self.assertIn("linked to a different child", self.notifier.sent[-1]["text"])
        self.assertEqual([(k, d) for k, d, _ in self.hubspot.calls], [("note", "1")])

        class Broken:
            def record_existing_child(self, *a):
                return lookup.FAILED
        store._reset_for_tests()
        job = self.run_live(plan(), registry_mod=Broken())
        self.assertEqual(job.status, store.DONE)
        self.assertIn("could not be updated", self.notifier.sent[-1]["text"])

    def test_a_registry_that_raises_while_writing_is_a_failed_code(self):
        with mock.patch.object(registry, "upsert_child", side_effect=OSError("disk")):
            self.assertEqual(lookup.record_existing_child("c1", {"name": "Zed Quill", "dob": DOB}, "HDCW", "1"),
                             lookup.FAILED)
            self.assertEqual(lookup.record_new_child("c1", "Zed Quill", DOB, "HDCW", URN, "1"), lookup.FAILED)
        self.assertEqual(lookup.record_existing_child("nope", None, "HDCW", "1"), lookup.MISSING)

    def test_dry_mode_writes_no_registry_row(self):
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "dry"}):
            store.enqueue("1", "dry")
            deps = self.deps(plan())
            deps.registry = mock.Mock(side_effect=AssertionError("dry mode wrote"))
            worker.run_once(deps)
            approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
            worker.run_once(deps)
        self.assertEqual((store.latest_for_deal("1").status, registry.stats()["children"]), (store.DRY_DONE, 0))

    def test_a_registry_hit_after_approval_is_asked_again_and_nothing_is_created(self):
        first, second = plan(child=()), plan()                 # approved as a NEW child; now the registry finds one
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps([first, second])
        deps.writes, deps.registry = self.writes, lookup
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        self.assertEqual((self.writes.calls, store.latest_for_deal("1").status), ([], store.AWAITING_APPROVAL))
        self.assertIn("changed since it was last approved", self.notifier.sent[-1]["text"])

    def test_the_similar_question_uses_the_registry_wording_and_names_stay_out_of_logs_and_jobs(self):
        p = plan(holds=["registry_similar_child"], child=(), candidates=["c1"])
        p["match"]["reason"] = "registry_similar"
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        _result, job = self.run_job(p)
        self.assertEqual((job.status, self.notifier.sent[-1]["kind"]), (store.AWAITING_APPROVAL, "match"))
        self.assertIn("similar name", self.notifier.sent[-1]["text"])
        row = repr(store.get(job.id))
        for text in (row, "\n".join(r.getMessage() for r in records)):
            self.assertNotIn("Zed", text)


class NewChildRegistryTests(TempRegistry, NewChildCase):
    def test_a_new_child_gets_a_registry_row_with_name_dob_urn_and_deal(self):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        deps.registry = lookup
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        job = store.latest_for_deal("1")
        child = registry.by_child_id("new-1")
        self.assertEqual((child.full_name, child.dob, child.urns, child.deals, child.source),
                         ("Zed Quill", DOB, [URN.casefold()], [("1", registry.ADHOC)], "adhoc"))
        self.assertIn("registry:added", job.outcomes)
        self.assertEqual(job.status, store.DONE)
        self.assertNotIn("Zed", repr(job))


if __name__ == "__main__":
    unittest.main()
