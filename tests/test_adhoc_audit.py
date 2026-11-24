"""adhoc-audit: session and enrolment codes, matching, read-only wiring. Fake data only."""

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from actions.adhoc_audit import runner
from actions.onboard_audit.runner import FamlyChild

TODAY = date(2026, 11, 24)          # a Tuesday
SITE = "site-1"


def deal(**over):
    base = {"id": "1", "dealstage": "s1", "famly_childid": "", "child_urn": "",
            "ad_hoc_date_1": "2026-12-01", "ad_hoc_session_1": "Morning",   # Tuesday
            "ad_hoc_end_date": "2026-12-01"}
    base.update(over)
    return base


class SessionCodeTests(unittest.TestCase):
    def codes(self, stage="Approved", **over):
        return runner.session_codes(deal(**over), stage, TODAY)[0]

    def test_clean_deal_has_no_codes(self):
        self.assertEqual(self.codes(), [])

    def test_slot_and_date_pairing(self):
        self.assertIn("session_slot_missing", self.codes(ad_hoc_session_1=""))
        self.assertIn("session_date_missing", self.codes(ad_hoc_date_1="", ad_hoc_session_2="Full Day"))
        self.assertIn("session_slot_unknown", self.codes(ad_hoc_session_1="Evening"))

    def test_dates_invalid_duplicate_past_weekend(self):
        self.assertIn("session_date_invalid", self.codes(ad_hoc_date_1="soon"))
        self.assertIn("session_date_duplicate",
                      self.codes(ad_hoc_date_2="2026-12-01", ad_hoc_session_2="Morning"))
        self.assertIn("session_date_past", self.codes(ad_hoc_date_1="2026-11-03"))
        self.assertIn("session_date_weekend", self.codes(ad_hoc_date_1="2026-12-05"))   # Saturday

    def test_past_is_fine_once_started(self):
        self.assertNotIn("session_date_past", self.codes(stage="Ad Hoc Completed", ad_hoc_date_1="2026-11-03"))

    def test_epoch_milliseconds_are_read(self):
        self.assertEqual(runner.parse_date("1796083200000"), date(2026, 12, 1))

    def test_end_date_and_no_sessions(self):
        self.assertIn("end_date_missing", self.codes(ad_hoc_end_date=""))
        self.assertIn("end_date_before_last_session", self.codes(ad_hoc_end_date="2026-11-30"))
        self.assertEqual(runner.session_codes({"id": "1"}, "Approved", TODAY)[0], ["no_sessions"])


class EnrolmentTests(unittest.TestCase):
    def child(self, relations):
        return FamlyChild(id="c1", relations=relations)

    def test_enrolment_checks(self):
        d = [date(2026, 12, 1)]
        self.assertEqual(runner.enrolment_codes(self.child([(SITE, "2025-01-01", "")]), SITE, d), [])
        self.assertEqual(runner.enrolment_codes(self.child([("other", "2025-01-01", "")]), SITE, d),
                         ["child_not_at_site"])
        self.assertEqual(runner.enrolment_codes(self.child([(SITE, "2025-01-01", "2026-07-31")]), SITE, d),
                         ["session_outside_enrolment"])


class FakeHubSpot:
    calls = 0

    def __init__(self, deals):
        self.deals = deals

    def stage_names(self):
        return {"s1": ("Ad Hoc", "Approved"), "s2": ("Operations", "Deposit Pending")}

    def search_deals(self, filters, properties, limit=100):
        wanted = set(filters[0]["values"])
        key = "dealstage" if filters[0]["propertyName"] == "dealstage" else "id"
        return [d for d in self.deals if d[key] in wanted]


class RunTests(unittest.TestCase):
    def run_audit(self, deals, children, plans=lambda cid: [], deal_ids=None):
        return runner.run(FakeHubSpot(deals), None, None, deal_ids=deal_ids, today=TODAY,
                          children=children, read_plans=plans)

    def test_matching_and_plan_codes(self):
        kid = FamlyChild(id="c1", relations=[(SITE, "2025-01-01", "")])
        deals = [deal(id="1", famly_childid="c1", location_of_interest="x"),
                 deal(id="2"), deal(id="3", famly_childid="gone")]
        summary, rows = self.run_audit(deals, {"c1": kid})
        by_id = {r.deal_id: r for r in rows}
        self.assertEqual(by_id["1"].child_id, "c1")
        self.assertIn("child_no_plan", by_id["1"].codes)
        self.assertIn("child_missing", by_id["2"].codes)
        self.assertIn("famly_childid_not_in_famly", by_id["3"].codes)
        self.assertEqual(summary["dealsRead"], 3)
        _s, rows = self.run_audit(deals[:1], {"c1": kid}, plans=lambda cid: ["plan"])
        self.assertIn("child_has_plan", rows[0].codes)

    def test_file_mode_flags_other_pipelines(self):
        _s, rows = self.run_audit([deal(id="9", dealstage="s2")], {}, deal_ids=["9"])
        self.assertEqual(rows[0].codes, ["not_ad_hoc_pipeline"])

    def test_csv_has_ids_and_codes_only(self):
        _s, rows = self.run_audit([deal(id="2")], {})
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "a.csv")
            runner.write_csv(path, rows)
            header = next(csv.reader(open(path, encoding="utf-8")))
        self.assertEqual(header[:7], ["dealId", "siteCode", "stage", "childId", "found_via", "sessions", "codes"])


if __name__ == "__main__":
    unittest.main()


class FakeContact:
    def __init__(self, email="", emails=()):
        self.email, self.emails = email, set(emails)


class ContactEmailTests(unittest.TestCase):
    def kid(self, *emails, cid="c1"):
        from actions.onboard_audit.runner import FamlyContact
        return FamlyChild(id=cid, relations=[(SITE, "2025-01-01", "")],
                          contacts=[FamlyContact(email=e) for e in emails])

    def audit(self, contacts, children, deals=None):
        deals = deals or [deal(id="1", famly_childid="c1")]
        return runner.run(FakeHubSpot(deals), None, None, today=TODAY, children=children,
                          read_plans=lambda c: [], contacts=True, read_contacts=lambda d: contacts)

    def test_match_mismatch_and_gaps(self):
        c = {"c1": self.kid("mum@x.org")}
        for contacts, expect in [
            ([FakeContact("Mum@X.org")], "contact_email_on_child"),
            ([FakeContact("other@x.org")], "contact_email_not_on_child"),
            ([FakeContact("")], "deal_no_contact_email"),
            ([], "deal_no_contacts"),
        ]:
            _s, rows = self.audit(contacts, c)
            self.assertIn(expect, rows[0].codes, expect)
        _s, rows = self.audit([FakeContact("a@x.org", ["mum@x.org"])], c)       # additional address
        self.assertIn("contact_email_on_child", rows[0].codes)

    def test_missing_child_is_found_by_parent_email(self):
        kids = {"c1": self.kid("mum@x.org"), "c2": self.kid("dad@x.org", "mum@x.org", cid="c2")}
        _s, rows = self.audit([FakeContact("mum@x.org")], kids, deals=[deal(id="2")])
        self.assertIn("email_matches_several_children", rows[0].codes)
        self.assertEqual(rows[0].candidates, ["c1", "c2"])
        _s, rows = self.audit([FakeContact("who@x.org")], kids, deals=[deal(id="2")])
        self.assertIn("email_no_match", rows[0].codes)

    def test_closed_and_completed_are_skipped(self):
        class HS(FakeHubSpot):
            def stage_names(self):
                return {"s1": ("Ad Hoc", "Approved"), "s9": ("Ad Hoc", "Ad Hoc Completed"),
                        "s8": ("Ad Hoc", "Closed Lost")}
        deals = [deal(id="1"), deal(id="2", dealstage="s9"), deal(id="3", dealstage="s8")]
        s, rows = runner.run(HS(deals), None, None, today=TODAY, children={}, read_plans=lambda c: [],
                             contacts=True, read_contacts=lambda d: [])
        self.assertEqual([r.deal_id for r in rows], ["1"])
