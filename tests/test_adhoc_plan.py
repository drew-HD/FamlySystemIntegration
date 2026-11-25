"""adhoc-dry-run planner: matching, ad hoc only, holds, pricing by age. Fake data only; reads only."""

import unittest
from unittest import mock
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


class SlackAnswerTests(Case):
    def test_candidates_are_reported_and_the_slack_answers_are_applied(self):
        other = kid(name="Ada Quill", birthday="2022-02-02")
        asked = self.plan([other])
        self.assertEqual(asked["match"]["candidateIds"], ["c1"])
        self.assertIn("parent_email_matches_a_different_child", asked["holds"])
        confirmed = planner.build_plan(deal(), "Approved", [Contact("mum@x.org")], Index({"c1": other}), self.deps(),
                                       TODAY, confirmed_child_id="c1")
        self.assertEqual((confirmed["match"]["via"], confirmed["match"]["childIds"], confirmed["holds"]),
                         ("slack_confirmed", ["c1"], []))
        new = planner.build_plan(deal(), "Approved", [Contact("mum@x.org")], Index({"c1": other}), self.deps(),
                                 TODAY, force_new=True)
        self.assertEqual((new["match"]["childIds"], new["holds"], new["steps"][0]),
                         ([], [], "create_child_no_end_date"))
        self.assertIn("new_child_confirmed_in_slack", new["flags"])

    def test_force_new_never_overrides_a_match_the_deal_itself_carries(self):
        plan = planner.build_plan(deal(famly_childid="c1"), "Approved", [Contact("mum@x.org")],
                                  Index({"c1": kid(emails=())}), self.deps(), TODAY, force_new=True)
        self.assertEqual(plan["match"]["childIds"], ["c1"])

    def deps(self):
        return planner.Deps(has_plan=lambda c: False, read_sessions=lambda c, d: LOOKUP,
                            preview=lambda c, p, d: {}, price_child_for_site=lambda s: "t")


class AllocatedUrnTests(Case):
    """An ad hoc child carries no deal reference; a Retry finds a child an earlier job made by the deal's allocated URN."""

    def planned(self, children, allocated, contacts=()):
        deps = planner.Deps(has_plan=lambda c: False, read_sessions=lambda c, d: LOOKUP, preview=lambda c, p, d: {},
                            price_child_for_site=lambda s: "t", allocated_urn=allocated)
        return planner.build_plan(deal(), "Approved", list(contacts), Index({c.id: c for c in children}), deps, TODAY)

    def test_the_child_with_the_allocated_urn_is_found(self):
        made = kid("made", emails=())
        made.external_id = "HD0042CW"
        plan = self.planned([made], lambda deal_id: "HD0042CW")
        self.assertEqual((plan["match"]["via"], plan["match"]["childIds"]), ("allocated_urn", ["made"]))

    def test_no_allocation_or_an_urn_nobody_carries_means_no_match(self):
        other = kid("other", emails=(), name="Someone Else", birthday="2020-01-01")
        other.external_id = "HD0001CW"
        for allocated in (lambda deal_id: None, lambda deal_id: "HD0042CW"):
            plan = self.planned([other], allocated)
            self.assertEqual((plan["match"]["via"], plan["match"]["childIds"]), ("none", []))

    def test_the_reader_builds_the_urn_from_the_onboard_store_exactly_as_the_allocator_does(self):
        from actions.adhoc_plan import readers
        from actions.onboard.urn_alloc import UrnService
        from actions.onboard_dry_run import urn as urn_fmt
        suffix = urn_fmt.site_suffix("HDCW")                       # the store holds the SUFFIX ("CW"), not the site code
        self.assertEqual(suffix, "CW")
        with mock.patch("actions.onboard.store.urn_allocation_for_deal", return_value=(suffix, 42)):
            self.assertEqual(readers.allocated_urn_for_deal("1"), "HD0042CW")
            self.assertEqual(readers.allocated_urn_for_deal("1"), UrnService(None, None).own("1"))   # same as `own`
        with mock.patch("actions.onboard.store.urn_allocation_for_deal", return_value=None):
            self.assertIsNone(readers.allocated_urn_for_deal("1"))


class ResumeContactsPlanTests(Case):
    """A child an earlier job made without its contacts: the plan carries the contacts and bill payer to create."""

    def planned(self, resume):
        made = kid("made", emails=())
        made.external_id = "HD0042CW"
        deps = planner.Deps(has_plan=lambda c: False, read_sessions=lambda c, d: LOOKUP, preview=lambda c, p, d: {},
                            price_child_for_site=lambda s: "t", allocated_urn=lambda deal_id: "HD0042CW",
                            resume_contacts=resume)
        contacts = [type("C", (), dict(hubspot_id="h1", is_bill_payer=True, email="a@x.org",
                                       has_first_and_last_name=True))()]
        return planner.build_plan(deal(), "Approved", contacts, Index({"made": made}), deps, TODAY)

    def test_resume_adds_the_contacts_section_and_the_step(self):
        plan = self.planned(lambda deal_id, child_id: True)
        self.assertEqual((plan["match"]["via"], plan["resumeContacts"]), ("allocated_urn", True))
        self.assertEqual(plan["contacts"], [{"hubspotContactId": "h1", "isBillPayer": True}])
        self.assertEqual(plan["steps"][0], "create_contacts")
        self.assertEqual(plan["holds"], [])

    def test_no_resume_means_the_plan_is_unchanged(self):
        for resume in (lambda deal_id, child_id: False, None):
            plan = self.planned(resume)
            self.assertNotIn("resumeContacts", plan)
            self.assertNotIn("contacts", plan)
            self.assertNotIn("create_contacts", plan["steps"])


class WhoAndStepsTests(Case):
    def test_existing_child_without_plan_is_ad_hoc_only(self):
        plan = self.plan([kid()])
        self.assertIs(plan["adHocOnly"], True)
        self.assertEqual(plan["steps"], ["assign_ad_hoc_only_tag", "remove_end_date_if_set", "book_2_sessions"])
        self.assertEqual(plan["holds"], [])

    def test_an_ended_enrolment_is_not_a_hold_the_session_is_just_booked(self):
        for plans in (False, True):
            plan = self.plan([kid(end="2026-07-31")], plans=plans)
            self.assertEqual(plan["holds"], [], plans)
            self.assertEqual(len(plan["bookings"]), 2)

    def test_child_with_a_plan_is_regular_no_tag(self):
        plan = self.plan([kid()], plans=True)
        self.assertIs(plan["adHocOnly"], False)
        self.assertEqual((plan["steps"], plan["holds"]), (["book_2_sessions"], []))

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

    def test_discount_fraction_becomes_a_flat_amount(self):
        plan = self.plan([kid()], adhoc_discount="0.1")             # 75.00 + 150.00 = 225.00
        self.assertEqual((plan["total"], plan["discountAmount"], plan["totalAfterDiscount"]),
                         ("225.00", "22.50", "202.50"))
        plan = self.plan([kid()], adhoc_discount="0.325")           # 73.125 -> rounds half up
        self.assertEqual(plan["discountAmount"], "73.13")
        self.assertEqual(self.plan([kid()])["discountAmount"], "0.00")

    def test_all_past_dates_hold_at_every_stage(self):
        for stage in ("Approved", "Ad Hoc Started", "Ad Hoc Completed", "Closed Lost"):
            plan = planner.build_plan(deal(ad_hoc_date_1="2026-11-02", ad_hoc_date_2="", ad_hoc_session_2=""), stage, [Contact("mum@x.org")],
                                      Index({"c1": kid()}), planner.Deps(
                                          has_plan=lambda c: False, read_sessions=lambda c, d: LOOKUP,
                                          preview=lambda c, p, d: {}, price_child_for_site=lambda s: "t"), TODAY)
            self.assertIn("no_future_sessions", plan["holds"], stage)
            self.assertNotIn("session_date_past", plan["holds"], stage)
            self.assertNotIn("purchase", plan, stage)

    def test_mixed_deal_books_only_future_sessions(self):
        # 2 Nov (past, Morning 75) + 28 Dec Morning 75 + 29 Dec Full Day 150; 10% discount on the booked two only
        plan = self.plan([kid()], adhoc_discount="0.1", ad_hoc_date_3="2026-11-02", ad_hoc_session_3="Morning")
        self.assertEqual(plan["holds"], [])
        self.assertEqual(plan["skippedPast"], ["2026-11-02"])
        self.assertIn("past_dates_skipped", plan["flags"])
        self.assertEqual([b["date"] for b in plan["bookings"]], ["2026-12-28", "2026-12-29"])
        self.assertEqual([b["purchaseDate"] for b in plan["purchase"]["sessionBookings"]], ["2026-12-28", "2026-12-29"])
        self.assertEqual((plan["total"], plan["discountAmount"], plan["totalAfterDiscount"]),
                         ("225.00", "22.50", "202.50"))
        self.assertEqual(plan["purchase"]["discountAmount"], "22.50")
        self.assertIn("book_2_sessions", plan["steps"])

    def test_today_counts_as_future(self):
        plan = self.plan([kid()], ad_hoc_date_1="2026-11-24", ad_hoc_session_1="Morning",
                         ad_hoc_date_2="2026-11-02", ad_hoc_session_2="Morning")
        self.assertEqual([b["date"] for b in plan["bookings"]], ["2026-11-24"])
        self.assertEqual(plan["skippedPast"], ["2026-11-02"])
        alone = self.plan([kid()], ad_hoc_date_1="2026-11-24", ad_hoc_date_2="", ad_hoc_session_2="")
        self.assertEqual((alone["holds"], "skippedPast" in alone), ([], False))

    def test_skipped_dates_are_not_judged_for_weekend_or_duplicate(self):
        # 2026-11-07 is a Saturday; the past weekend date and a past duplicate sit beside clean future sessions
        plan = self.plan([kid()], ad_hoc_date_3="2026-11-07", ad_hoc_session_3="Morning",
                         ad_hoc_date_4="2026-11-02", ad_hoc_session_4="Morning",
                         ad_hoc_date_5="2026-11-02", ad_hoc_session_5="Morning")
        self.assertEqual((plan["holds"], plan["details"]), ([], {}))
        self.assertEqual(plan["skippedPast"], ["2026-11-02", "2026-11-07"])
        weekend = self.plan([kid()], ad_hoc_date_3="2026-12-05", ad_hoc_session_3="Morning",
                            ad_hoc_date_4="2026-11-02", ad_hoc_session_4="Morning")
        self.assertIn("session_date_weekend", weekend["holds"])
        dup = self.plan([kid()], ad_hoc_date_3="2026-12-28", ad_hoc_session_3="Morning",
                        ad_hoc_date_4="2026-11-02", ad_hoc_session_4="Morning")
        self.assertIn("session_date_duplicate", dup["holds"])

    def test_skipped_past_changes_the_plan_hash(self):
        from actions.adhoc import worker
        a = self.plan([kid()], ad_hoc_date_3="2026-11-02", ad_hoc_session_3="Morning")
        b = dict(a, skippedPast=["2026-11-09"])
        self.assertNotEqual(worker.plan_hash(a), worker.plan_hash(b))

    def test_holds_say_which_dates_they_are_about(self):
        plan = self.plan([kid()], ad_hoc_date_1="2026-11-02", ad_hoc_date_2="2026-12-05", ad_hoc_session_2="Morning",
                         ad_hoc_date_3="2026-12-05", ad_hoc_session_3="Morning")
        self.assertEqual(plan["details"], {"session_date_duplicate": ["2026-12-05"],
                                           "session_date_weekend": ["2026-12-05"]})
        clean = self.plan([kid()])
        self.assertEqual(clean["details"], {})

    def test_a_conflict_names_the_dates_and_kinds(self):
        # booked: Morning on 28 Dec, Full Day on 29 Dec
        preview = {"newPurchaseConflictMessages": [{"date": "2026-12-28", "conflictKind": "ALREADY_BOOKED"}],
                   "unbookableSessions": [{"sessionId": "s-Morning", "conflictKind": "INPUT_PURCHASE", "date": "2026-12-28"},
                                          {"sessionId": "s-Full Day", "conflictKind": "ALREADY_BOOKED", "date": "2026-12-29"}]}
        plan = self.plan([kid()], preview=preview)
        self.assertEqual(plan["details"]["conflict_preview_not_clean"],
                         [{"date": "2026-12-28", "kind": "ALREADY_BOOKED"}, {"date": "2026-12-29", "kind": "ALREADY_BOOKED"}])

    def test_sessions_blocked_by_our_own_booking_on_other_days_are_not_conflicts(self):
        # Famly lists, on each booked date, the OTHER sessions that booking blocks. Morning is booked on 28 Dec, so on
        # 29 Dec (a Full Day date) the blocked Morning entry must NOT count: only (session, date) pairs we book do.
        preview = {"newPurchaseConflictMessages": [],
                   "unbookableSessions": [{"sessionId": "s-Full Day", "conflictKind": "INPUT_PURCHASE", "date": "2026-12-29"},
                                          {"sessionId": "s-Morning", "conflictKind": "ANOTHER_INPUT_PURCHASE", "date": "2026-12-29"},
                                          {"sessionId": "s-Full Day", "conflictKind": "ANOTHER_INPUT_PURCHASE", "date": "2026-12-28"},
                                          {"sessionId": "s-Morning", "conflictKind": "INPUT_PURCHASE", "date": "2026-12-28"}]}
        plan = self.plan([kid()], preview=preview)
        self.assertEqual((plan["holds"], plan["details"]), ([], {}))

    def test_a_session_booked_twice_on_a_date_is_still_a_conflict(self):
        preview = {"unbookableSessions": [{"sessionId": "s-Morning", "conflictKind": "ANOTHER_INPUT_PURCHASE",
                                           "date": "2026-12-28"}]}                 # Morning IS booked on 28 Dec
        self.assertIn("conflict_preview_not_clean", self.plan([kid()], preview=preview)["holds"])

    def test_money_and_booking_holds(self):
        for bad in ("abc", "10", "1.5", "-0.1"):
            self.assertIn("discount_invalid", self.plan([kid()], adhoc_discount=bad)["holds"], bad)
        self.assertEqual(self.plan([kid()], adhoc_discount="0.1")["holds"], [])
        self.assertEqual(self.plan([kid()], adhoc_discount="0")["holds"], [])
        self.assertIn("no_future_sessions", self.plan([kid()], ad_hoc_date_1="2026-11-02", ad_hoc_date_2="", ad_hoc_session_2="")["holds"])
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
