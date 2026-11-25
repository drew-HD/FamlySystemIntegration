"""Ad hoc: reopening a done deal by hand (`adhoc-enqueue --reopen-done`) and the Slack notice when the webhook sends a
done deal again. Fakes only."""

import argparse
import os
import tempfile
import unittest
from unittest import mock

from actions.adhoc import cli_enqueue, messages, store, web, worker
from tests.test_adhoc_worker import Case, plan


def done_job(deal_id="5"):
    job = store.enqueue(deal_id, "live").job
    store.set_status(job.id, store.DONE)
    return job


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"ADHOC_STORE_PATH": os.path.join(self.tmp.name, "j.sqlite3"),
                                               "ADHOC_MODE": "live"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class ReopenTests(Base):
    def args(self, **over):
        base = dict(deal_id=["5"], file=None, future=False, stage=None, limit=None, list_only=False, reopen_done=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_without_the_flag_a_done_deal_creates_no_job(self):
        done = done_job()
        out = cli_enqueue.handle(self.args())
        self.assertEqual((out["jobs"][0]["outcome"], out["jobs"][0]["jobId"]), (store.ALREADY_DONE, done.id))

    def test_reopen_done_creates_a_marked_job_and_never_duplicates_an_active_one(self):
        done_job()
        out = cli_enqueue.handle(self.args(reopen_done=True))
        job = store.get(out["jobs"][0]["jobId"])
        self.assertEqual((out["jobs"][0]["outcome"], job.status, job.reopen, out["jobs"][0]["reopened"]),
                         (store.CREATED, store.QUEUED, True, True))
        again = cli_enqueue.handle(self.args(reopen_done=True))
        self.assertEqual((again["jobs"][0]["outcome"], again["jobs"][0]["jobId"]), (store.ALREADY_QUEUED, job.id))

    def test_reopen_needs_deal_ids_and_never_comes_from_the_webhook(self):
        with self.assertRaises(SystemExit):
            cli_enqueue.handle(self.args(deal_id=None, file="x.txt", reopen_done=True))
        with self.assertRaises(ValueError):
            store.enqueue("5", "live", "webhook", reopen=True)
        out = cli_enqueue.handle(self.args(deal_id=["6"], reopen_done=True))      # never done: an ordinary job
        self.assertFalse(store.get(out["jobs"][0]["jobId"]).reopen)


class ReopenGateTests(Case):
    def test_the_approval_for_a_reopened_job_warns(self):
        done = store.enqueue("1", "dry").job
        store.set_status(done.id, store.DONE)
        store.enqueue("1", "dry", "cli", reopen=True)
        worker.run_once(self.deps(plan()))
        self.assertIn("Manual re-run", self.notifier.sent[-1]["text"])
        self.assertNotIn("Manual re-run", messages.gate_text(plan(), "h" * 16, "dry"))


class BlockedNoticeTests(Base):
    def test_the_webhook_on_a_done_deal_posts_a_notice_and_still_replies_200(self):
        done_job()
        with mock.patch("actions.adhoc.notifier.SlackNotifier.notice", return_value=True) as notice:
            data, status = web.handle({"dealId": "5"})
        self.assertEqual((data["outcome"], status), ("already_done", 200))
        text = notice.call_args[0][0]
        self.assertIn("already pushed to Famly", text)
        self.assertIn("adhoc-enqueue --deal-id 5 --reopen-done", text)

    def test_a_slack_failure_never_changes_the_reply(self):
        done_job()
        with mock.patch("actions.adhoc.notifier.SlackNotifier.notice", side_effect=RuntimeError("slack down")):
            self.assertEqual(web.handle({"dealId": "5"})[1], 200)

    def test_a_new_deal_posts_no_notice(self):
        with mock.patch("actions.adhoc.notifier.SlackNotifier.notice") as notice:
            self.assertEqual(web.handle({"dealId": "9"})[0]["outcome"], "queued")
        notice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
