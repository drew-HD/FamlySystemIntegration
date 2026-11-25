"""Children registry: store, name match, backfill (fakes only; temp store)."""

import csv
import os
import tempfile
import unittest
from pathlib import Path

from actions.onboard_audit.runner import FamlyChild, FamlyContact
from actions.registry import backfill, match, store

TEST_NAME = "Testchild Alpha"


class FakeHubSpot:
    def __init__(self, deals):
        self.deals = deals

    def stage_names(self):
        return {"ops": ("Operations", "Live"), "adhoc": ("Ad Hoc", "Approved")}

    def search_deals(self, filters, properties):
        stages = set(filters[0]["values"])
        return [d for d in self.deals if d["dealstage"] in stages]


def child(cid, name=TEST_NAME, dob="2024-01-02", urn="", refs=(), emails=()):
    return FamlyChild(id=cid, external_id=urn, birthday=dob, full_name=name,
                      relations=[("inst-cw", "2025-01-01", "")],
                      references=[("HUBSPOT", r) for r in refs],
                      contacts=[FamlyContact(email=e) for e in emails])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CHILDREN_REGISTRY_PATH")
        os.environ["CHILDREN_REGISTRY_PATH"] = str(Path(self.tmp.name) / "r.sqlite3")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("CHILDREN_REGISTRY_PATH", None)
        else:
            os.environ["CHILDREN_REGISTRY_PATH"] = self.old
        self.tmp.cleanup()


class StoreTests(Base):
    def test_urn_and_deal_belong_to_one_child_conflict_never_overwrites(self):
        store.upsert_child("c1", TEST_NAME, "2024-01-02", "HDCW", "test")
        store.upsert_child("c2", "Testchild Beta", "2024-01-02", "HDCW", "test")
        self.assertEqual(store.add_urn("c1", "HD0001CW"), store.ADDED)
        self.assertEqual(store.add_urn("c1", "hd0001cw"), store.ALREADY)
        self.assertEqual(store.add_urn("c2", "HD0001CW"), store.CONFLICT)
        self.assertEqual(store.add_deal("c1", "111", store.OPERATIONS), store.ADDED)
        self.assertEqual(store.add_deal("c2", "111", store.ADHOC), store.CONFLICT)
        self.assertEqual(store.by_urn("HD0001CW").famly_child_id, "c1")
        self.assertEqual(store.by_deal("111").famly_child_id, "c1")
        self.assertEqual(store.by_deal("111").deals, [("111", store.OPERATIONS)])

    def test_upsert_refreshes_from_famly_and_keeps_source(self):
        self.assertTrue(store.upsert_child("c1", "Old Name", "2024-01-02", "HDCW", "adhoc"))
        self.assertFalse(store.upsert_child("c1", TEST_NAME, "2024-01-03", None, "backfill"))
        got = store.by_child_id("c1")
        self.assertEqual((got.full_name, got.dob, got.site_code, got.source),
                         (TEST_NAME, "2024-01-03", "HDCW", "adhoc"))
        self.assertEqual(store.stats()["children"], 1)

    def test_email_may_be_on_siblings(self):
        store.upsert_child("c1", TEST_NAME, "2024-01-02", None, "test")
        store.upsert_child("c2", "Testchild Beta", "2022-05-05", None, "test")
        store.add_email("c1", "Parent@Example.test")
        store.add_email("c2", "parent@example.test")
        self.assertEqual({c.famly_child_id for c in store.by_email("PARENT@example.test")}, {"c1", "c2"})


class MatchTests(unittest.TestCase):
    def test_rules(self):
        d = "2024-01-02"
        self.assertEqual(match.compare("Alpha Testchild", "testchild ALPHA", d, d), match.EXACT)
        self.assertEqual(match.compare("Zoë Smith-Jones", "Zoe Smith Jones", d, d), match.EXACT)
        self.assertEqual(match.compare("Alpha Testchild", "Beta Testchild", d, d), match.SIMILAR)  # twins
        self.assertEqual(match.compare("Alpha Testchild", "Alpha Mae Testchild", d, d), match.SIMILAR)
        self.assertEqual(match.compare("Katherine Doe", "Katharine Dow", d, d), match.SIMILAR)  # spelling
        self.assertEqual(match.compare("Alpha Testchild", "Alpha Testchild", d, "2024-01-03"), match.NONE)
        self.assertEqual(match.compare("Alpha Testchild", "Gamma Other", d, d), match.NONE)
        self.assertEqual(match.compare("Alpha Testchild", "Alpha Testchild", "", ""), match.NONE)


class BackfillTests(Base):
    def run_backfill(self, children, deals, links=None, confirm=False):
        children = {c.id: c for c in children}
        return backfill.run(FakeHubSpot(deals), gql=None, confirm=confirm, children=children,
                            links=links or {}, site_codes={"inst-cw": "HDCW"})

    def test_links_by_reference_famly_childid_urn_and_jobs(self):
        kids = [child("c1", refs=["101"], urn="HD0001CW", emails=["p@example.test"]),
                child("c2", name="Testchild Beta", urn="HD0002CW"),
                child("c3", name="Testchild Gamma", urn="HD0003CW"),
                child("c4", name="Testchild Delta")]
        deals = [{"id": "101", "dealstage": "ops"},
                 {"id": "102", "dealstage": "ops", "famly_childid": "c2"},
                 {"id": "201", "dealstage": "adhoc", "child_urn": "hd0003cw"},
                 {"id": "202", "dealstage": "adhoc"},
                 {"id": "203", "dealstage": "adhoc"}]
        links = {"adhoc_jobs": [("202", "c4")], "urn_allocations": [("203", "HD0001CW")]}
        summary, report = self.run_backfill(kids, deals, links, confirm=True)
        self.assertEqual(summary["deals_to_link"], {"operations": 2, "adhoc": 3})
        self.assertEqual(store.by_deal("101").famly_child_id, "c1")
        self.assertEqual(store.by_deal("102").famly_child_id, "c2")
        self.assertEqual(store.by_deal("201").famly_child_id, "c3")
        self.assertEqual(store.by_deal("202").famly_child_id, "c4")
        self.assertEqual(store.by_deal("203").famly_child_id, "c1")
        c1 = store.by_child_id("c1")
        self.assertEqual((c1.full_name, c1.dob, c1.site_code, c1.urns), (TEST_NAME, "2024-01-02", "HDCW", ["hd0001cw"]))
        self.assertEqual(store.stats()["children"], 4)
        self.assertEqual(store.stats()["emails"], 1)

    def test_conflicts_are_reported_not_written(self):
        kids = [child("c1", refs=["101"], urn="HD0001CW"), child("c2", name="Testchild Beta", urn="HD0001CW")]
        deals = [{"id": "101", "dealstage": "ops", "famly_childid": "c2"},
                 {"id": "201", "dealstage": "adhoc", "child_urn": "HD0001CW"},
                 {"id": "202", "dealstage": "adhoc"}]
        summary, report = self.run_backfill(kids, deals, confirm=True)
        codes = summary["codes"]
        self.assertEqual(codes[backfill.DEAL_SEVERAL_CHILDREN], 2)
        self.assertEqual(codes[backfill.URN_SEVERAL_CHILDREN], 2)
        self.assertEqual(codes[backfill.STORE_CONFLICT], 1)           # the URN went to the first child only
        self.assertEqual(codes[f"{backfill.DEAL_NO_CHILD}:adhoc"], 2)  # 201 (URN on two) and 202
        self.assertIsNone(store.by_deal("101"))
        self.assertIsNone(store.by_deal("201"))

    def test_dry_run_writes_nothing(self):
        summary, _ = self.run_backfill([child("c1", refs=["101"])], [{"id": "101", "dealstage": "ops"}])
        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(summary["deals_to_link"], {"operations": 1})
        self.assertEqual(store.stats()["children"], 0)

    def test_csv_and_summary_hold_no_names_or_emails(self):
        kids = [child("c1", refs=["999"], emails=["p@example.test"], dob="")]
        summary, report = self.run_backfill(kids, [], confirm=True)
        out = Path(self.tmp.name) / "out.csv"
        backfill.write_csv(str(out), report)
        text = out.read_text() + repr(summary)
        for secret in (TEST_NAME, "Testchild", "p@example.test"):
            self.assertNotIn(secret, text)
        rows = list(csv.reader(out.open()))
        self.assertIn([backfill.REFERENCE_UNKNOWN_DEAL, "c1", "999"], rows)
        self.assertIn([backfill.CHILD_NO_DOB, "c1", ""], rows)


if __name__ == "__main__":
    unittest.main()
