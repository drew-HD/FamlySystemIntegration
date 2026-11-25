"""The ad hoc worker service loop: recover once, drain, sleep only when idle, survive errors, stop cleanly,
mode read each iteration. Fakes only."""

import logging
import os
import unittest
from unittest import mock

from actions.adhoc import worker


class Loop:
    """Drives run_forever with scripted run_once results and records sleeps."""

    def __init__(self, results, stop_after=None):
        self.results, self.sleeps, self.calls = list(results), [], 0
        self.stop_after = stop_after

    def run_once(self, deps=None):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"ran": r}

    def stop(self):
        return not self.results

    def run(self, **kw):
        with mock.patch.object(worker, "run_once", self.run_once), \
                mock.patch.object(worker, "recover") as rec:
            worker.run_forever(5.0, should_stop=kw.pop("should_stop", self.stop), sleep=self.sleeps.append, **kw)
        self.recover = rec


class ServiceLoopTests(unittest.TestCase):
    def setUp(self):
        prior = logging.root.manager.disable
        logging.disable(logging.NOTSET)           # other modules disable logging for the whole run
        self.addCleanup(logging.disable, prior)

    def test_recovers_once_and_drains_without_sleeping(self):
        loop = Loop([True, True, True])
        loop.run()
        loop.recover.assert_called_once()
        self.assertEqual((loop.calls, loop.sleeps), (3, []))

    def test_sleeps_only_when_idle(self):
        loop = Loop([True, False, True, False])
        loop.run()
        self.assertEqual(loop.sleeps, [5.0, 5.0])

    def test_exception_does_not_kill_loop_or_log_message(self):
        loop = Loop([ValueError("child Jane Doe secret"), True])
        with self.assertLogs("actions.adhoc.worker", level="INFO") as cm:
            loop.run()
        text = "\n".join(cm.output)
        self.assertIn("ValueError", text)
        self.assertNotIn("Jane", text)
        self.assertNotIn("secret", text)
        self.assertEqual(loop.sleeps, [worker.ERROR_BACKOFF_SECONDS])
        self.assertEqual(loop.calls, 2)

    def test_stop_signal_finishes_current_job_then_exits(self):
        state = {"stop": False}
        results = [True, True, True]

        def run_once(deps=None):
            results.pop(0)
            state["stop"] = True               # the signal arrives while this job runs
            return {"ran": True}
        with mock.patch.object(worker, "run_once", run_once), mock.patch.object(worker, "recover"):
            worker.run_forever(5.0, should_stop=lambda: state["stop"], sleep=lambda s: None)
        self.assertEqual(len(results), 2)      # exactly one job ran, and it ran to the end

    def test_sigterm_handler_sets_stop(self):
        stop = worker._Stop()
        self.assertFalse(stop())
        stop.request(15, None)
        self.assertTrue(stop())

    def test_mode_change_between_iterations_and_startup_log(self):
        modes = iter(["off", "dry", "dry"])
        seen = []

        def run_once(deps=None):
            seen.append(os.environ["ADHOC_MODE"])
            return {"ran": False}
        n = {"i": 0}

        def sleep(s):
            n["i"] += 1
            os.environ["ADHOC_MODE"] = next(modes)
        os.environ["ADHOC_MODE"] = "off"
        try:
            with mock.patch.object(worker, "run_once", run_once), mock.patch.object(worker, "recover"), \
                    self.assertLogs("actions.adhoc.worker", level="INFO") as cm:
                worker.run_forever(1.0, should_stop=lambda: n["i"] >= 3, sleep=sleep)
        finally:
            os.environ.pop("ADHOC_MODE", None)
        self.assertEqual(seen, ["off", "off", "dry"])
        text = "\n".join(cm.output)
        self.assertIn("mode=off", text)
        self.assertIn("idle", text)
        self.assertIn("mode is now dry", text)


if __name__ == "__main__":
    unittest.main()
