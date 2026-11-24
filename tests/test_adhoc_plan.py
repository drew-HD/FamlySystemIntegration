"""adhoc-dry-run planner: matching, ad hoc only, holds, pricing by age. Fake data only; reads only."""

import unittest
from datetime import date

from actions.adhoc_plan import planner
from actions.adhoc_probe import writer
from actions.onboard_audit.runner import FamlyChild, FamlyContact, Index
from integrations import catalogue

TODAY = date(2026, 11, 24)
CW = catalogue.load_sites()["HDCW"]["institutionId"]
U3, O3 = writer.PRICING_GROUPS["HDCW"]["under3"], writer.PRICING_GROUPS["HDCW"]["over3"]

LOOKUP = {"data": {"finance": {"revenueItems": {"productVersions": [], "sessionVersions": [
    {"title": t, "type": "StandaloneSession", "sessionId": f"s-{t}",
     "pricing": [{"adHocPrice": p, "pricingGroupId": U3}, {"adHocPrice": p, "pricingGroupId": O3}],
     "sessionAvailabilities": [{"day": d, "sessionAvailabilityId": f"{t}-{d}"} for d in ("MONDAY", "TUESDAY")]}
    for t, p in (("Morning", "75.00"), ("Afternoon", "75.00"), ("Full Day", "150.00"))]}}}}


class Contact:
    def __init__(self, email):
        self.email, self.emails = email, {email}


def kid(cid="c1", name="Zed Quill", birthday="2024-06-01", emails=("mum@x.org",), start="2025-01-01", end=""):
    return FamlyChild(id=cid, full_name=name, birthday=birthday, relations=[(CW, start, end)],
                      contacts=[FamlyContact(email=e) for e in emails])


def deal(**over):
    base = {"id": "100", "dealstage": "s", "location_of_interest": "Canada Water", "child_first_name": "Zed",
            "child_last_name": "Quill", "child_date_of_birth": "2024-06-01",
            "ad_hoc_date_1": "2026-12-28", "ad_hoc_session_1": "Morning",            # a Monday
            "ad_hoc_date_2": "2026-12-29", "ad_hoc_session_2": "Full Day"}            # a Tuesday
    base.update(over)
    return base


class Case(unittest.TestCase):
    def plan(self, children, contacts=(Contact("mum@x.org"),), plans=False, preview=None, **deal_over):
        self.calls = []
        deps = planner.Deps(
            has_plan=lambda cid: plans,
            read_sessions=lambda cid, day: LOOKUP,
            preview=lambda cid, purchases, day: (self.calls.append(cid) or preview or {}),
            price_child_for_site=lambda site: "test-child")
        index = Index({c.id: c for c in children})
        return planner.build_plan(deal(**deal_over), "Approved", list(contacts), index, deps, TODAY)


class MatchTests(Case):
    def test_matching_order_and_safety(self):
        self.assertEqual(self.plan([kid(emails=())], famly_childid="c1")["match"]["via"], "famly_childid")
        self.assertEqual(self.plan([kid()])["match"], {"via": "parent_email", "childIds": ["c1"]})
        # a parent's other child (different name AND dob) is never booked onto: a hold, not a guess
        other = kid(name="Ada Quill", birthday="2022-02-02")
        plan = self.plan([other])
        self.assertIn("parent_email_matches_a_different_child", plan["holds"])
        self.assertEqual(plan["match"]["childIds"], [])
        self.assertEqual(self.plan([kid(emails=("zed@x.org",))])["match"]["via"], "name_dob")
        self.assertEqual(self.plan([kid(emails=("zed@x.org",), name="Someone Else")])["match"]["via"], "none")

    def test_siblings_are_narrowed_by_name_and_dob(self):
        sibs = [kid("a", "Ada Quill", "2022-02-02"), kid("b")]
        self.assertEqual(self.plan(sibs)["match"]["childIds"], ["b"])
        self.assertIn("child_ambiguous", self.plan([kid("a"), kid("b")])["holds"])


class WhoAndStepsTests(Case):
    def test_existing_child_without_plan_is_ad_hoc_only(self):
        plan = self.plan([kid(end="2026-07-31")])
        self.assertIs(plan["adHocOnly"], True)
        self.assertEqual(plan["steps"], ["assign_ad_hoc_only_tag", "remove_end_date_if_set", "book_2_sessions"])
        self.assertEqual(plan["holds"], [])                   # the old end date no longer blocks: it is removed

    def test_child_with_a_plan_is_regular_no_tag_and_enrolment_must_cover(self):
        plan = self.plan([kid(end="2026-07-31")], plans=True)
        self.assertIs(plan["adHocOnly"], False)
        self.assertNotIn("assign_ad_hoc_only_tag", plan["steps"])
        self.assertIn("session_outside_enrolment", plan["holds"])
        self.assertEqual(self.plan([kid()], plans=True)["holds"], [])

    def test_new_child_is_created_without_an_end_date(self):
        plan = self.plan([], contacts=[Contact("new@x.org")])
        self.assertEqual(plan["steps"][:2], ["create_child_no_end_date", "create_contacts"])
        self.assertIn("conflicts_not_checked_new_child", plan["flags"])
        self.assertEqual(plan["holds"], [])

    def test_child_that_starts_after_the_first_session_is_held(self):
        self.assertIn("child_starts_after_first_session", self.plan([kid(start="2027-02-01")])["holds"])


class PricingAndHoldTests(Case):
    def test_prices_and_pricing_group_by_age_on_each_date(self):
        plan = self.plan([kid(birthday="2023-12-28")], child_date_of_birth="2023-12-28")   # turns 3 on 28 Dec
        first, second = plan["bookings"]
        self.assertEqual((first["price"], first["ageBand"], first["pricingGroup"]), ("75.00", "over3", O3))
        self.assertEqual((second["price"], second["session"]), ("150.00", "Full Day"))
        self.assertEqual(plan["total"], "225.00")
        self.assertEqual(self.plan([kid()])["bookings"][0]["ageBand"], "under3")

    def test_money_and_booking_holds(self):
        self.assertIn("discount_unconfirmed", self.plan([kid()], adhoc_discount="10")["holds"])
        self.assertNotIn("discount_unconfirmed", self.plan([kid()], adhoc_discount="0")["holds"])
        self.assertIn("session_date_past", self.plan([kid()], ad_hoc_date_1="2026-11-02")["holds"])
        self.assertIn("session_slot_missing", self.plan([kid()], ad_hoc_session_1="")["holds"])
        self.assertIn("site_unmapped", self.plan([kid()], location_of_interest="Mars")["holds"])
        self.assertIn("conflict_preview_not_clean",
                      self.plan([kid()], preview={"unbookableSessions": [{"sessionId": "s-Morning",
                                                                         "conflictKind": "ALREADY_BOOKED"}]})["holds"])
        clean = {"unbookableSessions": [{"sessionId": "s-Morning", "conflictKind": "INPUT_PURCHASE"}]}
        self.assertEqual(self.plan([kid()], preview=clean)["holds"], [])

    def test_output_never_carries_names_or_emails(self):
        text = "\n".join(planner.describe(self.plan([kid()]))) + repr(self.plan([kid()]))
        for secret in ("Zed", "Quill", "mum@x.org", "2024-06-01"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
