"""famly_plan_status on the AD HOC deal after a booking attempt: plan_created when booked cleanly, needs_attention (with
the codes) when the booking was sent but needs a person, nothing for holds, rejected bookings or dry mode. A failed
HubSpot write is a Slack line, never a stop. Fakes only."""

import os
import unittest
from unittest import mock

from actions.adhoc import approval, deal_writeback, store, worker
from actions.adhoc import writes as writes_mod
from core.hubspot import HubSpotError
from integrations import slack
from tests.test_adhoc_worker import Case, FakeWrites, plan


class StatusWriter:
    def __init__(self, result="status_written"):
        self.calls, self.result = [], result

    def set_plan_status(self, deal_id, status, note):
        self.calls.append((deal_id, status, note))
        return self.result


class PlanStatusTests(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ADHOC_MODE": "live"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_live(self, writes, p=None, writer=None, mode="live"):
        self.status = writer or StatusWriter()
        with mock.patch.dict(os.environ, {"ADHOC_MODE": mode}):
            store.enqueue("1", mode)
            deps = self.deps(p or plan())
            deps.writes, deps.deal_writer = writes, self.status
            worker.run_once(deps)
            approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
            worker.run_once(deps)
        return store.latest_for_deal("1")

    def test_a_clean_booking_sets_plan_created_and_clears_the_note(self):
        job = self.run_live(FakeWrites())
        self.assertEqual(self.status.calls, [("1", "plan_created", "")])
        self.assertEqual(job.status, store.DONE)
        self.assertIn("hubspot:status_written:plan_created", job.outcomes)
        self.assertIn("famly_plan_status set to plan_created", self.notifier.sent[-1]["text"])

    def test_a_booking_that_needs_a_person_sets_needs_attention_with_the_codes(self):
        job = self.run_live(FakeWrites(book=writes_mod.BookingResult(["p1"], False, ["discount"]), tag="tag_failed"))
        self.assertEqual(self.status.calls, [("1", "needs_attention", "booking_mismatch:discount,tag_failed")])
        self.assertEqual(job.status, store.NEEDS_REVIEW)

    def test_an_unknown_booking_outcome_sets_needs_attention(self):
        job = self.run_live(FakeWrites(book=writes_mod.BookingUnknown()))
        self.assertEqual(self.status.calls, [("1", "needs_attention", "booking_outcome_unknown")])
        self.assertIn("famly_plan_status set to needs_attention", self.notifier.sent[-1]["text"])
        self.assertEqual(job.status, store.NEEDS_REVIEW)

    def test_a_rejected_booking_a_hold_and_dry_mode_write_nothing(self):
        self.run_live(FakeWrites(book=writes_mod.BookingRejected("booking_rejected")))
        self.assertEqual(self.status.calls, [])
        store._reset_for_tests()
        self.status = StatusWriter()
        store.enqueue("1", "live")
        deps = self.deps(plan(holds=["no_sessions"]))
        deps.deal_writer = self.status
        worker.run_once(deps)
        self.assertEqual(self.status.calls, [])
        store._reset_for_tests()
        self.run_live(mock.Mock(side_effect=AssertionError("wrote")), mode="dry")
        self.assertEqual(self.status.calls, [])

    def test_a_failed_status_write_is_a_slack_line_and_the_job_is_still_done(self):
        job = self.run_live(FakeWrites(), writer=StatusWriter("status_failed"))
        self.assertEqual(job.status, store.DONE)
        self.assertIn("Needs checking: famly_plan_status could not be set", self.notifier.sent[-1]["text"])

    def test_the_note_is_a_code_of_at_most_100_characters(self):
        self.assertEqual(worker.status_note(["A b", "c"]), "a_b,c")
        long = worker.status_note([f"code_number_{i}" for i in range(20)])
        self.assertLessEqual(len(long), 100)
        self.assertRegex(long, r"^[a-z0-9_:.,+-]+,\+\d+$")


class DealWriterStatusTests(unittest.TestCase):
    class Client:
        def __init__(self, fail=False):
            self.calls, self.fail = [], fail

        def get_deal(self, deal_id): return {}
        def write_deal_properties(self, deal_id, props): pass

        def write_plan_status(self, deal_id, value):
            self.calls.append(("status", value))
            if self.fail:
                raise HubSpotError("boom", 500)

        def write_plan_note(self, deal_id, note): self.calls.append(("note", note))

    def test_one_request_each_never_raises(self):
        client = self.Client()
        self.assertEqual(deal_writeback.DealWriter(client).set_plan_status("1", "plan_created", ""), "status_written")
        self.assertEqual(client.calls, [("status", "plan_created"), ("note", "")])
        bad = self.Client(fail=True)
        self.assertEqual(deal_writeback.DealWriter(bad).set_plan_status("1", "plan_created", ""), "status_failed")
        self.assertEqual(bad.calls, [("status", "plan_created")])


if __name__ == "__main__":
    unittest.main()
