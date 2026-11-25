"""adhoc_enqueue webhook and the --future backfill. Fakes only."""

import argparse
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import web_registry
from actions.adhoc import cli_enqueue, store, web

TODAY = date(2026, 11, 25)
STAGES = {"1": ("Ad Hoc", "Approved"), "2": ("Ad Hoc", "Closed Lost"), "3": ("Ad Hoc", "Ad Hoc Completed"),
          "4": ("Ad Hoc", "Booked"), "9": ("Operations", "Deposit Pending")}


class FakeHubSpot:
    def __init__(self, deals):
        self.deals = deals

    def stage_names(self):
        return STAGES

    def search_deals(self, filters, properties):
        ids = set(filters[0]["values"])
        return [d for d in self.deals if d["dealstage"] in ids]


def deal(i, stage, *dates):
    d = {"id": i, "dealstage": stage}
    d.update({f"ad_hoc_date_{n}": v for n, v in enumerate(dates, 1)})
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.dict(os.environ, {"ADHOC_STORE_PATH": os.path.join(self.tmp.name, "j.sqlite3"),
                                         "ADHOC_MODE": "dry"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class WebTests(Base):
    def test_registered(self):
        self.assertIn("adhoc_enqueue", web_registry.ACTIONS if hasattr(web_registry, "ACTIONS") else
                      str(vars(web_registry)))

    def test_bad_deal_ids(self):
        for body in ({}, {"dealId": "12a"}, {"dealId": ""}, {"dealId": 1.5}, {"dealId": None}):
            self.assertEqual(web.handle(body)[1], 400, body)
        self.assertIsNone(store.latest_for_deal("12"))

    def test_good_then_duplicate(self):
        data, status = web.handle({"dealId": "77"})
        self.assertEqual((status, data["outcome"]), (200, "queued"))
        self.assertEqual(store.latest_for_deal("77").source, "webhook")
        data, status = web.handle({"dealId": 77})
        self.assertEqual((status, data["outcome"]), (200, "already_queued"))

    def test_done_deal(self):
        job = store.enqueue("5", "dry").job
        store.set_status(job.id, store.DONE)
        self.assertEqual(web.handle({"dealId": "5"})[0]["outcome"], "already_done")

    def test_mode_off_refuses(self):
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "off"}):
            data, status = web.handle({"dealId": "8"})
        self.assertEqual((status, data["outcome"], data["jobId"]), (200, "refused_mode_off", None))
        self.assertIsNone(store.latest_for_deal("8"))

    def test_internal_error_is_500(self):
        with mock.patch.object(store, "enqueue", side_effect=RuntimeError("x")):
            self.assertEqual(web.handle({"dealId": "8"})[1], 500)


class BackfillTests(Base):
    def setUp(self):
        super().setUp()
        self.hs = FakeHubSpot([
            deal("10", "1", "2026-11-25", "2026-12-01"),      # all future: in
            deal("11", "1", "2026-11-01", "2026-12-01"),      # mixed: in (one date today or later)
            deal("12", "1", "2026-11-01"),                    # past
            deal("13", "1"),                                  # no dates
            deal("14", "2", "2026-12-01"),                    # Closed Lost
            deal("15", "3", "2026-12-01"),                    # Completed
            deal("16", "4", "2026-12-05"),                    # in, other stage
            deal("17", "1", "2026-12-01"),                    # active job
            deal("18", "1", "2026-12-01"),                    # done job
        ])
        store.enqueue("17", "dry")
        store.set_status(store.enqueue("18", "dry").job.id, store.DONE)

    def test_filter_and_enqueue(self):
        r = cli_enqueue.backfill(self.hs, "dry", today=TODAY)
        self.assertEqual(r["dealIds"], ["10", "11", "16"])
        self.assertEqual(r["byStage"], {"Approved": 2, "Booked": 1})
        self.assertEqual(store.latest_for_deal("10").source, "cli")
        self.assertIsNotNone(store.active_for_deal("16"))

    def test_stage_and_limit(self):
        r = cli_enqueue.backfill(self.hs, "dry", today=TODAY, stage_names=["booked"], list_only=True)
        self.assertEqual(r["dealIds"], ["16"])
        r = cli_enqueue.backfill(self.hs, "dry", today=TODAY, limit=1, list_only=True)
        self.assertEqual(r["dealIds"], ["10"])

    def test_list_only_enqueues_nothing(self):
        r = cli_enqueue.backfill(self.hs, "dry", today=TODAY, list_only=True)
        self.assertEqual(r["dealIds"], ["10", "11", "16"])
        self.assertIsNone(store.latest_for_deal("10"))
        self.assertNotIn("jobs", r)


if __name__ == "__main__":
    unittest.main()
