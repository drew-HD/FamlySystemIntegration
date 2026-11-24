"""Ad hoc job store and worker (step 1, dry mode): one active job per deal, claim order, holds, mode gates, and that
the worker is read-only by construction (its only dependency is a planner function). Fake data only."""

import argparse
import os
import tempfile
import unittest
from unittest import mock

from actions.adhoc import cli_enqueue, config, store, worker


def plan(holds=(), flags=("stage_not_approved",), stage="Approved", child=("c1",)):
    return {"dealId": "1", "stage": stage, "holds": list(holds), "flags": list(flags),
            "match": {"via": "urn", "childIds": list(child)}, "adHocOnly": True,
            "bookings": [{"date": "2026-12-28", "session": "Morning", "price": "75.00"}],
            "total": "75.00", "discountAmount": "0.00", "steps": ["book_1_sessions"]}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"ADHOC_STORE_PATH": os.path.join(self.tmp.name, "j.sqlite3"),
                                               "ADHOC_MODE": "dry"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_job(self, plan_or_error, deal="1", source="cli"):
        store.enqueue(deal, "dry", source)
        result = worker.run_once(worker.Deps(plan_deal=lambda d: plan_or_error))
        return result, store.get(result["jobId"])


class StoreTests(Case):
    def test_one_active_job_per_deal_but_a_child_can_have_many_deals(self):
        first = store.enqueue("1", "dry")
        self.assertEqual((first.outcome, store.enqueue("1", "dry").outcome), (store.CREATED, store.ALREADY_QUEUED))
        self.assertEqual(store.enqueue("2", "dry").outcome, store.CREATED)
        store.set_status(first.job.id, store.DONE)
        self.assertEqual(store.enqueue("1", "dry").outcome, store.ALREADY_DONE)
        with self.assertRaises(ValueError):
            store.enqueue("3", "dry", "email")

    def test_a_finished_job_does_not_block_a_fresh_one_and_claims_are_oldest_first(self):
        a = store.enqueue("1", "dry").job
        store.enqueue("2", "dry")
        self.assertEqual(store.claim_next().id, a.id)
        store.set_status(a.id, store.HELD)
        self.assertEqual(store.enqueue("1", "dry").outcome, store.CREATED)
        self.assertEqual(store.claim_next().deal_id, "2")
        self.assertEqual(store.claim_next().deal_id, "1")        # the fresh job for deal 1
        self.assertIsNone(store.claim_next())

    def test_a_job_caught_mid_write_is_never_replayed(self):
        job = store.enqueue("1", "dry").job
        store.set_status(job.id, store.WRITING)
        self.assertEqual(store.recover_in_flight(), 1)
        after = store.get(job.id)
        self.assertEqual((after.status, after.last_error_code), (store.NEEDS_REVIEW, "interrupted"))
        with self.assertRaises(ValueError):
            store.update(job.id, status="done")                  # status only moves through set_status


class WorkerTests(Case):
    def test_mode_off_and_live_do_nothing(self):
        store.enqueue("1", "dry")
        for mode, expect in (("off", None), ("live", "live_not_built")):
            with mock.patch.dict(os.environ, {"ADHOC_MODE": mode}):
                result = worker.run_once(worker.Deps(plan_deal=lambda d: self.fail("must not plan")))
            self.assertEqual((result["ran"], result.get("refused")), (False, expect))
        self.assertEqual(store.status_counts(), {store.QUEUED: 1})

    def test_clean_plan_is_dry_done_and_recorded(self):
        _result, job = self.run_job(plan())
        self.assertEqual((job.status, job.holds, job.child_id), (store.DRY_DONE, [], "c1"))
        self.assertEqual(job.plan["total"], "75.00")
        self.assertEqual(len(job.plan_hash), 16)

    def test_plan_with_holds_is_held_and_a_new_child_has_no_child_id(self):
        _r, job = self.run_job(plan(holds=["discount_invalid", "session_date_past"], child=()))
        self.assertEqual((job.status, job.holds, job.child_id), (store.HELD, ["discount_invalid", "session_date_past"], None))

    def test_a_webhook_job_must_be_an_approved_deal_a_cli_job_may_be_any_stage(self):
        _r, job = self.run_job(plan(stage="Invoice Paid"), source="webhook")
        self.assertEqual((job.status, job.holds), (store.HELD, [worker.STAGE_NOT_ELIGIBLE]))
        _r, job = self.run_job(plan(stage="Approved"), deal="2", source="webhook")
        self.assertEqual((job.status, job.flags), (store.DRY_DONE, []))
        _r, job = self.run_job(plan(stage="Invoice Paid"), deal="3", source="cli")
        self.assertEqual((job.status, job.flags), (store.DRY_DONE, ["stage_not_approved"]))

    def test_unreadable_deals_and_worker_errors_are_codes_never_messages(self):
        _r, job = self.run_job({"error": "deal_not_found"})
        self.assertEqual((job.status, job.last_error_code), (store.FAILED, "deal_not_found"))
        _r, job = self.run_job({"error": "not_ad_hoc_pipeline"}, deal="2")
        self.assertEqual((job.status, job.holds), (store.HELD, ["not_ad_hoc_pipeline"]))
        store.enqueue("3", "dry")

        def boom(deal):
            raise RuntimeError("secret body with a name")
        worker.run_once(worker.Deps(plan_deal=boom))
        job = store.latest_for_deal("3")
        self.assertEqual((job.status, job.last_error_code), (store.FAILED, "worker_error"))
        self.assertNotIn("secret", repr(job))


class CliTests(Case):
    def test_enqueue_is_refused_while_the_mode_is_off(self):
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "off"}), self.assertRaises(SystemExit):
            cli_enqueue.handle(argparse.Namespace(deal_id=["1"], file=None))
        self.assertEqual(store.status_counts(), {})
        out = cli_enqueue.handle(argparse.Namespace(deal_id=["1", "2"], file=None))
        self.assertEqual([j["outcome"] for j in out["jobs"]], ["created", "created"])


if __name__ == "__main__":
    unittest.main()
