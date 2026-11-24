"""Ad hoc job store, worker and Slack gate (dry mode): one active job per deal, holds, the plan approval and the
child-match question, click safety (nonce, atomic, stale), and that nothing here can write to Famly or HubSpot (the
worker's only dependencies are a planner function and a notifier). Fake data only."""

import argparse
import os
import tempfile
import unittest
from unittest import mock

from actions.adhoc import approval, cli_enqueue, store, worker
from integrations import slack


def plan(holds=(), flags=("stage_not_approved",), stage="Approved", child=("c1",), candidates=None, total="75.00"):
    match = {"via": "urn", "childIds": list(child)}
    if candidates is not None:
        match["candidateIds"] = list(candidates)
    return {"dealId": "1", "stage": stage, "site": "HDCW", "holds": list(holds), "flags": list(flags), "match": match,
            "adHocOnly": True, "total": total, "discountAmount": "0.00", "discountFraction": "0",
            "totalAfterDiscount": total, "steps": ["book_1_sessions"],
            "bookings": [{"date": "2026-12-28", "session": "Morning", "price": "75.00", "ageBand": "under3",
                          "pricingGroup": "g"}]}


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def _post(self, kind, text, **ids):
        self.sent.append({"kind": kind, "text": text, **ids})
        return None if self.fail else f"C1:{len(self.sent)}"

    def gate(self, text, job_id, plan_hash, nonce): return self._post("gate", text, job=job_id, hash=plan_hash, nonce=nonce)
    def match(self, text, job_id, plan_hash, nonce): return self._post("match", text, job=job_id, hash=plan_hash, nonce=nonce)
    def hold(self, text, job_id, nonce): return self._post("hold", text, job=job_id, hash="", nonce=nonce)
    def notice(self, text): return self._post("notice", text) is not None

    def value(self, index=-1):
        m = self.sent[index]
        return approval.button_value(m["job"], m["hash"], m["nonce"])


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"ADHOC_STORE_PATH": os.path.join(self.tmp.name, "j.sqlite3"),
                                               "ADHOC_MODE": "dry"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.notifier = FakeNotifier()
        self.calls = []

    def deps(self, plans):
        """`plans`: a plan or error dict, or a list consumed one per call."""
        seq = plans if isinstance(plans, list) else None

        def plan_deal(deal_id, **kwargs):
            self.calls.append(kwargs)
            return seq.pop(0) if seq and len(seq) > 1 else (seq[0] if seq else plans)

        return worker.Deps(plan_deal=plan_deal, child_name=lambda c: "Zed Quill", notifier=self.notifier)

    def run_job(self, plans, deal="1", source="cli"):
        store.enqueue(deal, "dry", source)
        result = worker.run_once(self.deps(plans))
        return result, store.get(result["jobId"])

    def step(self, plans):
        return worker.run_once(self.deps(plans))


class StoreTests(Case):
    def test_one_active_job_per_deal_but_a_child_can_have_many_deals(self):
        first = store.enqueue("1", "dry")
        self.assertEqual((first.outcome, store.enqueue("1", "dry").outcome), (store.CREATED, store.ALREADY_QUEUED))
        self.assertEqual(store.enqueue("2", "dry").outcome, store.CREATED)
        store.set_status(first.job.id, store.DONE)
        self.assertEqual(store.enqueue("1", "dry").outcome, store.ALREADY_DONE)
        with self.assertRaises(ValueError):
            store.enqueue("3", "dry", "email")

    def test_claims_are_oldest_first_and_a_finished_job_does_not_block_a_fresh_one(self):
        a = store.enqueue("1", "dry").job
        store.enqueue("2", "dry")
        self.assertEqual(store.claim_next().id, a.id)
        store.set_status(a.id, store.HELD)
        self.assertEqual(store.enqueue("1", "dry").outcome, store.CREATED)
        self.assertEqual([store.claim_next().deal_id, store.claim_next().deal_id], ["2", "1"])
        self.assertIsNone(store.claim_next())

    def test_a_job_caught_mid_write_is_never_replayed_and_status_only_moves_through_set_status(self):
        job = store.enqueue("1", "dry").job
        store.set_status(job.id, store.WRITING)
        self.assertEqual(store.recover_in_flight(), 1)
        after = store.get(job.id)
        self.assertEqual((after.status, after.last_error_code), (store.NEEDS_REVIEW, "interrupted"))
        with self.assertRaises(ValueError):
            store.update(job.id, status="done")

    def test_a_step_1_store_without_the_gate_columns_is_migrated(self):
        import sqlite3
        path = store.store_path()
        connection = sqlite3.connect(path)
        connection.executescript("CREATE TABLE adhoc_jobs (id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, "
                                 "status TEXT NOT NULL, mode TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'cli', "
                                 "plan_hash TEXT, plan_json TEXT, holds TEXT, flags TEXT, child_id TEXT, "
                                 "attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, claimed_at TEXT, "
                                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);")
        connection.close()
        self.assertEqual(store.enqueue("1", "dry").outcome, store.CREATED)
        self.assertIsNone(store.claim_next().approval_nonce)


class GateTests(Case):
    def test_mode_off_and_live_do_nothing(self):
        store.enqueue("1", "dry")
        for mode, expect in (("off", None), ("live", "live_not_built")):
            with mock.patch.dict(os.environ, {"ADHOC_MODE": mode}):
                result = worker.run_once(self.deps(plan()))
            self.assertEqual((result["ran"], result.get("refused")), (False, expect))
        self.assertEqual((store.status_counts(), self.notifier.sent), ({store.QUEUED: 1}, []))

    def test_a_clean_plan_posts_the_approval_and_waits(self):
        _r, job = self.run_job(plan())
        self.assertEqual((job.status, job.question, job.holds), (store.AWAITING_APPROVAL, "plan", []))
        sent = self.notifier.sent
        self.assertEqual([m["kind"] for m in sent], ["gate"])
        self.assertIn("[DRY RUN]", sent[0]["text"])
        self.assertIn("£75.00", sent[0]["text"])
        self.assertTrue(job.approval_nonce and job.approval_ref)
        for secret in ("Zed", "Quill"):
            self.assertNotIn(secret, sent[0]["text"])

    def test_holds_are_a_plain_notice_with_retry_and_a_webhook_job_must_be_approved(self):
        _r, job = self.run_job(plan(holds=["discount_invalid", "session_date_past"], child=()))
        self.assertEqual((job.status, job.holds, job.child_id), (store.HELD, ["discount_invalid", "session_date_past"], None))
        self.assertEqual(self.notifier.sent[-1]["kind"], "hold")
        self.assertIn("A booked date is in the past.", self.notifier.sent[-1]["text"])
        _r, job = self.run_job(plan(stage="Invoice Paid"), deal="2", source="webhook")
        self.assertEqual((job.status, job.holds), (store.HELD, [worker.STAGE_NOT_ELIGIBLE]))
        _r, job = self.run_job(plan(stage="Invoice Paid"), deal="3", source="cli")
        self.assertEqual((job.status, job.flags), (store.AWAITING_APPROVAL, ["stage_not_approved"]))

    def test_unreadable_deals_and_worker_errors_are_codes_never_messages(self):
        _r, job = self.run_job({"error": "deal_not_found"})
        self.assertEqual((job.status, job.last_error_code), (store.FAILED, "deal_not_found"))
        _r, job = self.run_job({"error": "not_ad_hoc_pipeline"}, deal="2")
        self.assertEqual((job.status, job.holds), (store.HELD, ["not_ad_hoc_pipeline"]))
        store.enqueue("3", "dry")

        def boom(deal, **kw):
            raise RuntimeError("secret body with a name")
        worker.run_once(worker.Deps(plan_deal=boom, notifier=self.notifier))
        job = store.latest_for_deal("3")
        self.assertEqual((job.status, job.last_error_code), (store.FAILED, "worker_error"))
        self.assertNotIn("secret", repr(job))

    def test_if_slack_refuses_the_post_the_job_is_held_not_left_waiting(self):
        self.notifier.fail = True
        _r, job = self.run_job(plan())
        self.assertEqual((job.status, job.last_error_code), (store.HELD, "slack_post_failed"))


class ApprovalTests(Case):
    def waiting(self, p=None):
        _r, job = self.run_job(p or plan())
        return job

    def test_approve_requeues_the_worker_rechecks_and_a_dry_run_writes_nothing(self):
        job = self.waiting()
        text = approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u1")
        self.assertEqual(text, approval.MSG_APPROVED)
        queued = store.get(job.id)
        self.assertEqual((queued.status, queued.approved_hash), (store.QUEUED, job.plan_hash))
        self.step(plan())
        self.assertEqual(store.get(job.id).status, store.DRY_DONE)
        self.assertEqual(self.notifier.sent[-1]["kind"], "notice")
        # a double click, or a click once the job moved on, does nothing
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(0), "u2"),
                         approval.MSG_HANDLED)

    def test_a_stale_nonce_or_hash_or_the_wrong_button_is_refused(self):
        job = self.waiting()
        good = self.notifier.sent[0]
        for value in (approval.button_value(job.id, good["hash"], "wrong"), approval.button_value(job.id, "0" * 16, good["nonce"]),
                      "not-a-job:abc:def", None):
            text = approval.handle_click(slack.ACTION_ADHOC_APPROVE, value, "u")
            self.assertIn(text, (approval.MSG_STALE, approval.MSG_UNKNOWN))
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_MATCH_YES, self.notifier.value(), "u"), approval.MSG_STALE)
        self.assertEqual(store.get(job.id).status, store.AWAITING_APPROVAL)

    def test_reject_holds_the_job_and_nothing_more_happens(self):
        job = self.waiting()
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_REJECT, self.notifier.value(), "u"), approval.MSG_REJECTED)
        after = store.get(job.id)
        self.assertEqual((after.status, after.last_error_code), (store.HELD, "approval_rejected"))
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_REJECT, self.notifier.value(), "u"), approval.MSG_HANDLED)

    def test_a_plan_that_changes_after_approval_is_asked_again_once_then_needs_review(self):
        job = self.waiting(plan(total="75.00"))
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        self.step(plan(total="80.00"))                                      # changed since approval
        again = store.get(job.id)
        self.assertEqual((again.status, again.reask_count), (store.AWAITING_APPROVAL, 1))
        self.assertIn("changed since it was last approved", self.notifier.sent[-1]["text"])
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        self.step(plan(total="85.00"))                                      # and again
        self.assertEqual((store.get(job.id).status, store.get(job.id).last_error_code),
                         (store.NEEDS_REVIEW, worker.PLAN_UNSTABLE))

    def test_the_child_match_question_names_the_famly_child_and_records_the_answer(self):
        mismatch = plan(holds=[worker.MATCH_HOLD], child=(), candidates=["c9"])
        job = self.waiting(mismatch)
        self.assertEqual((job.status, job.question, job.candidate_child_id), (store.AWAITING_APPROVAL, "match", "c9"))
        question = self.notifier.sent[-1]
        self.assertEqual(question["kind"], "match")
        self.assertIn("Zed Quill", question["text"])                          # the Famly name: the one allowed name
        self.assertIn("deal", question["text"].lower())
        self.assertNotIn("Zed", repr(store.get(job.id)) + str(store.get(job.id).plan))   # never stored
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_MATCH_YES, self.notifier.value(), "u"), approval.MSG_MATCH_YES)
        self.calls.clear()
        self.step(plan(child=("c9",)))                                        # planned again WITH the answer
        self.assertEqual(self.calls[0], {"confirmed_child_id": "c9", "force_new": False})
        self.assertEqual((store.get(job.id).status, store.get(job.id).question), (store.AWAITING_APPROVAL, "plan"))

    def test_create_a_new_child_is_passed_to_the_planner(self):
        job = self.waiting(plan(holds=[worker.MATCH_HOLD], child=(), candidates=["c9"]))
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_MATCH_NEW, self.notifier.value(), "u"), approval.MSG_MATCH_NEW)
        self.calls.clear()
        self.step(plan(child=()))
        self.assertEqual(self.calls[0], {"confirmed_child_id": None, "force_new": True})
        self.assertEqual(store.get(job.id).question, "plan")

    def test_a_mismatch_with_several_candidates_or_other_holds_gets_no_question(self):
        _r, job = self.run_job(plan(holds=[worker.MATCH_HOLD], child=(), candidates=["a", "b"]))
        self.assertEqual((job.status, self.notifier.sent[-1]["kind"]), (store.HELD, "hold"))
        _r, job = self.run_job(plan(holds=[worker.MATCH_HOLD, "session_date_past"], child=(), candidates=["a"]), deal="2")
        self.assertEqual((job.status, self.notifier.sent[-1]["kind"]), (store.HELD, "hold"))

    def test_retry_on_a_hold_starts_a_fresh_job_once(self):
        _r, job = self.run_job(plan(holds=["session_date_past"]))
        value = self.notifier.value()
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_RETRY, value, "u"), approval.MSG_RETRY_QUEUED)
        self.assertEqual(store.active_for_deal("1").status, store.QUEUED)
        self.assertEqual(approval.handle_click(slack.ACTION_ADHOC_RETRY, value, "u"), approval.MSG_RETRY_HANDLED)
        bad = approval.button_value(job.id, "", "nope")
        self.assertIn(approval.handle_click(slack.ACTION_ADHOC_RETRY, bad, "u"),
                      (approval.MSG_RETRY_STALE, approval.MSG_RETRY_HANDLED))
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "off"}):
            self.assertIn(approval.handle_click(slack.ACTION_ADHOC_RETRY, value, "u"),
                          (approval.MSG_RETRY_OFF, approval.MSG_RETRY_HANDLED))


class SlackWiringTests(unittest.TestCase):
    def test_the_posters_build_the_right_buttons_and_clicks_are_routed_to_the_ad_hoc_handler(self):
        seen = {}

        def fake_post(text, blocks, label):
            seen[label] = [b["action_id"] for b in blocks[1]["elements"]]
            return "C:1"
        with mock.patch.object(slack, "_post_onboard_blocks", fake_post):
            slack.post_adhoc_gate("t", "v")
            slack.post_adhoc_match("t", "v")
            slack.post_adhoc_hold("t", "v")
        self.assertEqual(seen["ad hoc approval request"], [slack.ACTION_ADHOC_APPROVE, slack.ACTION_ADHOC_REJECT])
        self.assertEqual(seen["ad hoc child-match question"],
                         [slack.ACTION_ADHOC_MATCH_YES, slack.ACTION_ADHOC_MATCH_NEW, slack.ACTION_ADHOC_REJECT])
        self.assertEqual(seen["ad hoc hold notice"], [slack.ACTION_ADHOC_RETRY])
        import web_registry
        with mock.patch.object(web_registry.adhoc_approval, "handle_click", return_value="ok") as handler:
            for action in slack.ADHOC_ACTIONS:
                self.assertEqual(web_registry.handle_slack_click(action, "v", "u"), "ok")
        self.assertEqual(handler.call_count, len(slack.ADHOC_ACTIONS))


class CliTests(Case):
    def test_enqueue_is_refused_while_the_mode_is_off(self):
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "off"}), self.assertRaises(SystemExit):
            cli_enqueue.handle(argparse.Namespace(deal_id=["1"], file=None))
        self.assertEqual(store.status_counts(), {})
        out = cli_enqueue.handle(argparse.Namespace(deal_id=["1", "2"], file=None))
        self.assertEqual([j["outcome"] for j in out["jobs"]], ["created", "created"])


if __name__ == "__main__":
    unittest.main()
