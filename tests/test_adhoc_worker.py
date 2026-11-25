"""Ad hoc job store, worker and Slack gate (dry mode): one active job per deal, holds, the plan approval and the
child-match question, click safety (nonce, atomic, stale), and that nothing here can write to Famly or HubSpot (the
worker's only dependencies are a planner function and a notifier). Fake data only."""

import argparse
import logging
import os
import tempfile
import unittest
from unittest import mock

from actions.adhoc import approval, cli_enqueue, store, worker
from actions.adhoc import writes as writes_mod
from integrations import slack


def plan(holds=(), flags=("stage_not_approved",), stage="Approved", child=("c1",), candidates=None, total="75.00",
         discount=("0", "0.00")):
    match = {"via": "urn", "childIds": list(child)}
    if candidates is not None:
        match["candidateIds"] = list(candidates)
    purchase = {"childId": child[0] if child else None, "discountAmount": discount[1], "productBookings": [],
                "sessionBookings": [{"bookedPrice": "75.00", "pricingGroupId": "g", "purchaseDate": "2026-12-28",
                                     "sessionAvailabilityId": "sa-1"}]}
    return {"dealId": "1", "stage": stage, "site": "HDCW", "siteId": "inst-cw", "purchase": purchase,
            "holds": list(holds), "flags": list(flags), "match": match,
            "adHocOnly": True, "total": total, "discountAmount": discount[1], "discountFraction": discount[0],
            "totalAfterDiscount": f"{float(total) - float(discount[1]):.2f}", "steps": ["book_1_sessions"],
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
    def test_mode_off_does_nothing(self):
        store.enqueue("1", "dry")
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "off"}):
            result = worker.run_once(self.deps(plan()))
        self.assertEqual(result["ran"], False)
        self.assertEqual((store.status_counts(), self.notifier.sent), ({store.QUEUED: 1}, []))

    def test_a_clean_plan_posts_the_approval_and_waits(self):
        _r, job = self.run_job(plan(discount=("0.05", "3.75")))
        self.assertEqual((job.status, job.question, job.holds), (store.AWAITING_APPROVAL, "plan", []))
        sent = self.notifier.sent
        self.assertEqual([m["kind"] for m in sent], ["gate"])
        self.assertTrue(job.approval_nonce and job.approval_ref)
        self.assertEqual(sent[0]["text"].split("\n"), [
            "*[DRY RUN] Ad hoc approval: book 1 session*",
            "Deal: 1 · Site: Canada Water",
            "Child exists in Famly: Zed Quill (Famly Id: c1)",
            "Ad hoc only child",
            "1. 28 Dec 2026 Morning £75.00",
            "Total £75.00 · Discount 5% (-£3.75) · To pay £71.25",
            "Steps: book_1_sessions",
            f"Plan: `{job.plan_hash}`"])
        self.assertNotIn("Zed", repr(store.get(job.id)) + str(store.get(job.id).plan))   # the name is never stored

    def test_message_wording_for_a_new_child_a_regular_child_and_the_other_sites(self):
        from actions.adhoc import messages
        new = plan(child=())
        self.assertIn("Child doesn't exist in Famly, to be created", messages.gate_text(new, "h" * 16, "dry"))
        self.assertNotIn("Zed", messages.gate_text(new, "h" * 16, "dry", child_name="Zed Quill"))
        regular = dict(plan(), adHocOnly=False, site="HDWEST")
        text = messages.gate_text(regular, "h" * 16, "dry", child_name="Zed Quill")
        self.assertIn("Site: Westminster", text)
        self.assertIn("\nStandard child booking ad hoc\n", text)
        self.assertNotIn("Who:", text)
        self.assertNotIn("Discount", text)                                   # no discount line when there is none
        self.assertEqual((messages.site_name("HDCITY"), messages.long_date("2026-12-05"), messages.percent("0.325"),
                          messages.percent("0.1"), messages.percent("0.025")), ("City", "05 Dec 2026", "32.5%", "10%", "2.5%"))

    def test_holds_are_a_plain_notice_with_retry_and_a_webhook_job_must_be_approved(self):
        _r, job = self.run_job(plan(holds=["discount_invalid", "session_date_past"], child=()))
        self.assertEqual((job.status, job.holds, job.child_id), (store.HELD, ["discount_invalid", "session_date_past"], None))
        self.assertEqual(self.notifier.sent[-1]["kind"], "hold")
        self.assertIn("A booked date is in the past.", self.notifier.sent[-1]["text"])
        _r, job = self.run_job(plan(stage="Invoice Paid"), deal="2", source="webhook")
        self.assertEqual((job.status, job.holds), (store.HELD, [worker.STAGE_NOT_ELIGIBLE]))
        _r, job = self.run_job(plan(stage="Invoice Paid"), deal="3", source="cli")
        self.assertEqual((job.status, job.flags), (store.AWAITING_APPROVAL, ["stage_not_approved"]))

    def test_the_hold_notice_lists_the_dates_and_what_kind_of_conflict(self):
        held = dict(plan(holds=["session_date_past", "conflict_preview_not_clean"], child=()),
                    details={"session_date_past": ["2026-11-02", "2026-11-09"],
                             "conflict_preview_not_clean": [{"date": "2026-12-28", "kind": "ALREADY_BOOKED"},
                                                            {"date": "2026-12-29", "kind": "ANOTHER_INPUT_PURCHASE"}]})
        self.run_job(held)
        text = self.notifier.sent[-1]["text"]
        self.assertIn("A booked date is in the past (02 Nov 2026, 09 Nov 2026).", text)
        self.assertIn("Famly reports a booking conflict for these dates: 28 Dec 2026 (already booked); "
                      "29 Dec 2026 (another session on this booking is on the same day).", text)

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


class FakeWrites:
    """Stands in for actions.adhoc.writes.FamlyWriter; records every call."""

    def __init__(self, book=None, tag="tag_added", end="end_date_removed"):
        self.calls, self._book, self._tag, self._end = [], book, tag, end

    def book(self, child_id, purchase):
        self.calls.append(("book", child_id, purchase))
        if isinstance(self._book, Exception):
            raise self._book
        return self._book or writes_mod.BookingResult(["p1"], True)

    def tag(self, child_id, institution_id):
        self.calls.append(("tag", child_id, institution_id))
        return self._tag

    def remove_end_date(self, child_id):
        self.calls.append(("end", child_id))
        return self._end


class LiveWriteTests(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ADHOC_MODE": "live"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def approved_run(self, writes, p=None):
        """Plan, approve in Slack, then the worker writes. Returns the final job."""
        p = p or plan()
        store.enqueue("1", "live")
        deps = self.deps(p)
        deps.writes = writes
        worker.run_once(deps)                                        # plans, asks
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)                                        # re-plans, unchanged: writes
        return store.latest_for_deal("1")

    def test_an_approved_plan_books_exactly_the_approved_purchase_then_tags_and_removes_the_end_date(self):
        w = FakeWrites()
        job = self.approved_run(w)
        self.assertEqual([c[0] for c in w.calls], ["book", "tag", "end"])
        self.assertEqual(w.calls[0][2], plan()["purchase"])          # the exact approved purchase, nothing rebuilt
        self.assertEqual(w.calls[1][1:], ("c1", "inst-cw"))
        self.assertEqual((job.status, job.write_sent, job.outcomes), (store.DONE, True, ["booked:1", "tag_added", "end_date_removed"]))
        self.assertIn("✅ Ad hoc done", self.notifier.sent[-1]["text"])

    def test_a_standard_child_is_booked_but_not_tagged_and_keeps_their_enrolment(self):
        w = FakeWrites()
        job = self.approved_run(w, dict(plan(), adHocOnly=False))
        self.assertEqual(([c[0] for c in w.calls], job.status), (["book"], store.DONE))

    def test_a_rejected_booking_fails_and_nothing_else_is_written(self):
        w = FakeWrites(book=writes_mod.BookingRejected("booking_rejected"))
        job = self.approved_run(w)
        self.assertEqual(([c[0] for c in w.calls], job.status, job.last_error_code), (["book"], store.FAILED, "booking_rejected"))

    def test_an_unclear_booking_outcome_needs_a_person_and_is_never_sent_again(self):
        w = FakeWrites(book=writes_mod.BookingUnknown())
        job = self.approved_run(w)
        self.assertEqual(([c[0] for c in w.calls], job.status, job.last_error_code),
                         (["book"], store.NEEDS_REVIEW, "booking_outcome_unknown"))
        self.assertEqual(store.recover_in_flight(), 0)
        self.assertIsNone(store.claim_next())                         # nothing re-queues it
        self.assertIn("NOT sent again", self.notifier.sent[-1]["text"])

    def test_a_booking_that_does_not_match_the_approval_or_a_failed_tag_ends_needs_review(self):
        w = FakeWrites(book=writes_mod.BookingResult(["p1"], False, ["discount"]), tag="tag_failed")
        job = self.approved_run(w)
        self.assertEqual(job.status, store.NEEDS_REVIEW)
        self.assertEqual(job.last_error_code, "booking_mismatch:discount,tag_failed")
        self.assertEqual([c[0] for c in w.calls], ["book", "tag", "end"])        # the booking is done: the rest still ran

    def test_a_job_that_already_sent_its_booking_never_sends_another(self):
        w = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(plan())
        deps.writes = w
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        job = store.latest_for_deal("1")
        self.assertTrue(store.mark_write_sent(job.id))                # an earlier attempt already sent it
        worker.run_once(deps)
        after = store.latest_for_deal("1")
        self.assertEqual((w.calls, after.status, after.last_error_code), ([], store.NEEDS_REVIEW, "duplicate_write_blocked"))

    def test_dry_mode_never_touches_the_writer(self):
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "dry"}):
            store.enqueue("1", "dry")
            deps = self.deps(plan())
            deps.writes = mock.Mock(side_effect=AssertionError("dry mode wrote"))
            worker.run_once(deps)
            approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
            worker.run_once(deps)
        self.assertEqual(store.latest_for_deal("1").status, store.DRY_DONE)
        self.assertEqual(deps.writes.mock_calls, [])

    def test_a_worker_that_died_mid_write_is_never_replayed(self):
        job = store.enqueue("1", "live").job
        store.set_status(job.id, store.WRITING)
        store.mark_write_sent(job.id)
        self.assertEqual(store.recover_in_flight(), 1)
        self.assertEqual(store.get(job.id).last_error_code, "interrupted")
        self.assertIsNone(store.claim_next())


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


class ExceptionAfterWriteTests(LiveWriteTests):
    """An exception after the booking request was sent ends needs_review; before it, failed. Never retried."""

    SECRET = "Jane Doe jane@example.com"

    def run_checked(self, w, notifier_raises=False):
        with self.assertLogs("actions.adhoc.worker", level="INFO") as cm:
            job = self.approved_run(w)
        return job, "\n".join(cm.output)

    def assert_clean(self, job, logs):
        self.assertNotIn("Jane", logs)
        self.assertNotIn("example.com", logs)
        self.assertNotIn("Jane", repr(job))
        self.assertNotIn("example.com", repr(job))
        self.assertNotIn("Jane", "".join(m["text"] for m in self.notifier.sent))

    def test_exception_after_booking_is_needs_review_and_is_never_rebooked(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.root.manager.disable)

        class W(FakeWrites):
            def tag(s, child_id, institution_id):
                s.calls.append(("tag", child_id, institution_id))
                raise RuntimeError(ExceptionAfterWriteTests.SECRET)
        w = W()
        job, logs = self.run_checked(w)
        self.assertEqual((job.status, job.last_error_code, job.write_sent),
                         (store.NEEDS_REVIEW, "worker_error_after_write", True))
        self.assertIn("RuntimeError", logs)
        self.assert_clean(job, logs)
        self.assertIn("NOT sent again", self.notifier.sent[-1]["text"])
        self.assertIsNone(store.claim_next())                       # never re-claimed
        worker.run_once(self.deps(plan()))
        self.assertEqual([c[0] for c in w.calls].count("book"), 1)  # no second book call

    def test_notifier_raising_in_the_done_notice_after_booking_is_needs_review(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        w = FakeWrites()

        class Boom(FakeNotifier):
            def notice(s, text):
                raise ValueError(ExceptionAfterWriteTests.SECRET)
        store.enqueue("1", "live")
        self.notifier = Boom()
        deps = self.deps(plan())
        deps.writes = w
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        with self.assertLogs("actions.adhoc.worker", level="INFO") as cm:
            worker.run_once(deps)                                    # must not raise
        job = store.latest_for_deal("1")
        self.assertEqual((job.status, job.last_error_code, job.write_sent),
                         (store.NEEDS_REVIEW, "worker_error_after_write", True))
        logs = "\n".join(cm.output)
        self.assertIn("ValueError", logs)
        self.assert_clean(job, logs)
        self.assertEqual([c[0] for c in w.calls].count("book"), 1)
        self.assertIsNone(store.claim_next())

    def test_exception_before_the_booking_was_sent_is_still_failed(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        w = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(plan())
        deps.writes = w
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        with mock.patch.object(store, "mark_write_sent", side_effect=RuntimeError(self.SECRET)), \
                self.assertLogs("actions.adhoc.worker", level="INFO") as cm:
            worker.run_once(deps)
        job = store.latest_for_deal("1")
        self.assertEqual((job.status, job.last_error_code, job.write_sent), (store.FAILED, "worker_error", False))
        self.assertEqual(w.calls, [])
        self.assert_clean(job, "\n".join(cm.output))

    def test_a_failing_status_update_does_not_escape(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        store.enqueue("1", "live")
        deps = self.deps(plan())
        with mock.patch.object(worker, "_process", side_effect=RuntimeError(self.SECRET)), \
                mock.patch.object(store, "set_status", side_effect=OSError(self.SECRET)):
            worker.run_once(deps)                                    # must not raise


class SkippedPastMessageTests(unittest.TestCase):
    def test_gate_text_lists_the_skipped_past_dates(self):
        from actions.adhoc import messages
        p = plan()
        self.assertNotIn("Past dates not booked", messages.gate_text(p, "a" * 16, "dry"))
        p["skippedPast"] = ["2026-11-02", "2026-11-09"]
        self.assertIn("Past dates not booked: 02 Nov 2026, 09 Nov 2026", messages.gate_text(p, "a" * 16, "dry"))
