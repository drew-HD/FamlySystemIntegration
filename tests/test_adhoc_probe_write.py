"""adhoc-probe-write: every guard on the write path. Fakes only; nothing here can reach Famly."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from actions.adhoc_probe import writer

TODAY = date(2026, 11, 24)
HDCW = "11111111-1111-1111-1111-111111111111"
INST = "inst-cw"
TAG = "tag-1"
GROUP_U3 = writer.PRICING_GROUPS["HDCW"]["under3"]
GROUP_O3 = writer.PRICING_GROUPS["HDCW"]["over3"]

LOOKUP = {"data": {"finance": {"revenueItems": {"productVersions": [], "sessionVersions": [
    {"title": "Morning", "type": "StandaloneSession", "sessionId": "s-morning",
     "pricing": [{"adHocPrice": "75.00", "pricingGroupId": GROUP_U3}, {"adHocPrice": "75.00", "pricingGroupId": GROUP_O3}],
     "sessionAvailabilities": [{"day": "MONDAY", "sessionAvailabilityId": "sa-mon"}]},
    {"title": "(F) Morning", "type": "StandaloneSession", "sessionId": "s-f",
     "pricing": [{"adHocPrice": None, "pricingGroupId": GROUP_U3}], "sessionAvailabilities": []}]}}}}


class FakeRest:
    def __init__(self, test=True, end="2030-08-31", tags=None, found_tags=None):
        self.test, self.end, self.tags = test, end, list(tags or [])
        self.found_tags = found_tags if found_tags is not None else [
            {"tagId": TAG, "title": "Ad Hoc Only", "institutionId": INST, "deletedAt": None}]
        self.posts = []

    def get(self, path, params=None, **kw):
        if path.endswith("/summary"):
            return {"child": {"isTestChild": self.test, "endDate": self.end, "localEndDate": self.end or "",
                              "localStartDate": "2026-01-05", "birthday": "2024-06-01T00:00:00+00:00"},
                    "tags": [{"tagId": t} for t in self.tags]}
        return self.found_tags

    def post(self, path, params=None, json_body=None):
        self.posts.append((path, json_body))
        self.tags.append(path.split("/")[3])
        return {}


class FakeGql:
    def __init__(self, preview=None, upsert_ids=("p1",)):
        self.calls, self.preview, self.upsert_ids = [], preview or {}, list(upsert_ids)

    def execute(self, query_path, variables, operation_name):
        self.calls.append((operation_name, variables))
        if operation_name == "GetSessionsAndProducts":
            return LOOKUP
        if operation_name == "GetAdHocPurchaseSessionsConflicts":
            return {"data": {"finance": {"purchases": {"previewUpdate": self.preview}}}}
        if operation_name == "UpsertPurchase":
            return {"data": {"finance": {"purchases": {"update": [{"id": i, "sessionBookings": []}
                                                                   for i in self.upsert_ids]}}}}
        return {"data": {"finance": {"purchases": {"delete": True}}}}

    def ops(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def go(self, steps, rest=None, gql=None, confirm=True, site="HDCW"):
        rest, gql, out = rest or FakeRest(), gql or FakeGql(), []
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "t.json"
            store.write_text(json.dumps({"HDCW": HDCW}))
            with mock.patch.object(writer.catalogue, "load_sites", lambda: {"HDCW": {"institutionId": INST}}):
                result = writer.run(confirm, steps, site, rest, gql, out.append, today=TODAY, store_path=store)
        return result, rest, gql, out


class GateTests(Base):
    def test_confirm_and_step_are_required_and_nothing_is_sent(self):
        for kwargs in ({"confirm": False, "steps": ["tag"]}, {"confirm": True, "steps": []},
                       {"confirm": True, "steps": ["delete-all"]}):
            gql = FakeGql()
            with self.assertRaises(writer.WriteRefused):
                self.go(kwargs["steps"], gql=gql, confirm=kwargs["confirm"])
            self.assertEqual(gql.calls, [])

    def test_a_child_that_is_not_a_test_child_is_never_written(self):
        gql = FakeGql()
        with self.assertRaises(writer.WriteRefused):
            self.go(["end-date"], rest=FakeRest(test=False), gql=gql)
        self.assertEqual(gql.calls, [])

    def test_writer_refuses_unknown_child_and_bad_purchases_and_foreign_deletes(self):
        w = writer.Writer(FakeRest(), FakeGql(), {"HDCW": HDCW})
        with self.assertRaises(writer.WriteRefused):
            w.remove_end_date("some-real-child", "2026-01-05")
        with self.assertRaises(writer.WriteRefused):
            w.upsert(HDCW, {"childId": "other", "productBookings": []})
        with self.assertRaises(writer.WriteRefused):
            w.upsert(HDCW, {"childId": HDCW, "productBookings": [{"x": 1}]})
        with self.assertRaises(writer.WriteRefused):
            w.delete(HDCW, ["purchase-not-from-this-run"])
        self.assertEqual(w.writes, 0)

    def test_write_cap(self):
        w = writer.Writer(FakeRest(), FakeGql(), {"HDCW": HDCW})
        w.writes = writer.MAX_WRITES
        with self.assertRaises(writer.WriteRefused):
            w.remove_end_date(HDCW, "2026-01-05")


class StepTests(Base):
    def test_end_date_removal_sends_start_date_only(self):
        _r, _rest, gql, _out = self.go(["end-date"])
        _op, variables = gql.calls[0]
        self.assertEqual(variables["childActiveDates"], {"startDate": "2026-01-05"})
        self.assertIs(variables["isParent"], False)

    def test_end_date_already_empty_is_a_no_op(self):
        _r, _rest, gql, _out = self.go(["end-date"], rest=FakeRest(end=None))
        self.assertEqual(gql.calls, [])

    def test_tag_assigned_once_and_only_when_exactly_one_live_tag(self):
        _r, rest, _g, _o = self.go(["tag"])
        self.assertEqual(rest.posts, [(f"/v2/tags/{TAG}/assign", {"childId": HDCW})])
        _r, rest, _g, _o = self.go(["tag"], rest=FakeRest(tags=[TAG]))
        self.assertEqual(rest.posts, [])
        for tags in ([], [{"tagId": "a", "title": "Ad Hoc Only", "institutionId": INST},
                          {"tagId": "b", "title": "Ad Hoc Only", "institutionId": INST}]):
            result, rest, _g, _o = self.go(["tag"], rest=FakeRest(found_tags=tags))
            self.assertEqual(rest.posts, [])
            self.assertTrue(result["outcome"]["tag"]["HDCW"].startswith("stopped"))


class BookingTests(Base):
    def test_booking_cycle_previews_books_one_monday_then_deletes_only_its_own_purchase(self):
        result, _rest, gql, _out = self.go(["booking"])
        self.assertEqual(gql.ops(), ["GetSessionsAndProducts", "GetAdHocPurchaseSessionsConflicts",
                                     "UpsertPurchase", "DeletePurchase"])
        purchase = gql.calls[2][1]["purchases"][0]
        booking = purchase["sessionBookings"][0]
        self.assertEqual((booking["bookedPrice"], booking["pricingGroupId"], booking["sessionAvailabilityId"]),
                         ("75.00", GROUP_U3, "sa-mon"))                 # a 2-year-old: under-3 group
        self.assertEqual(date.fromisoformat(booking["purchaseDate"]).weekday(), 0)
        self.assertEqual((purchase["productBookings"], purchase["discountAmount"]), ([], "0"))
        self.assertEqual(gql.calls[3][1], {"purchaseIds": ["p1"]})
        self.assertEqual(result["writeRequests"], 2)

    def test_a_dirty_preview_or_a_blocked_session_stops_before_any_write(self):
        for preview in ({"newPurchaseConflictMessages": [{"severity": "ERROR"}]},
                        {"unbookableSessions": [{"sessionId": "s-morning", "conflictKind": "ALREADY_BOOKED"}]},
                        {"unbookableSessions": [{"sessionId": "s-morning", "conflictKind": "ANOTHER_INPUT_PURCHASE"}]}):
            result, _rest, gql, _out = self.go(["booking"], gql=FakeGql(preview=preview))
            self.assertNotIn("UpsertPurchase", gql.ops())
            self.assertEqual(result["writeRequests"], 0)

    def test_the_session_being_booked_marked_input_purchase_is_not_a_conflict(self):
        preview = {"newPurchaseConflictMessages": [],
                   "unbookableSessions": [{"sessionId": "s-morning", "conflictKind": "INPUT_PURCHASE"},
                                          {"sessionId": "s-other", "conflictKind": "ANOTHER_INPUT_PURCHASE"}]}
        _r, _rest, gql, _out = self.go(["booking"], gql=FakeGql(preview=preview))
        self.assertIn("UpsertPurchase", gql.ops())
        self.assertEqual(gql.ops()[-1], "DeletePurchase")

    def test_pricing_group_by_age_and_hard_errors(self):
        self.assertEqual(writer.age_band(date(2023, 11, 25), TODAY), "under3")
        self.assertEqual(writer.age_band(date(2023, 11, 24), TODAY), "over3")      # turns 3 today
        self.assertEqual(writer.pricing_group_for("HDCW", date(2020, 1, 1), TODAY), GROUP_O3)
        self.assertTrue(writer.pricing_group_for("HDWEST", date(2024, 1, 1), TODAY).startswith("3be14e3c"))
        self.assertTrue(writer.pricing_group_for("HDWEST", date(2020, 1, 1), TODAY).startswith("3773609b"))
        self.assertTrue(writer.pricing_group_for("HDCITY", date(2024, 1, 1), TODAY).startswith("18b50271"))
        self.assertTrue(writer.pricing_group_for("HDCITY", date(2020, 1, 1), TODAY).startswith("78d46103"))
        with self.assertRaises(writer.WriteRefused):
            writer.pricing_group_for("HDELSEWHERE", date(2024, 1, 1), TODAY)       # unmapped site
        bad = json.loads(json.dumps(LOOKUP))
        bad["data"]["finance"]["revenueItems"]["sessionVersions"][0]["pricing"][0]["adHocPrice"] = None
        with self.assertRaises(writer.WriteRefused):
            writer.build_purchase(HDCW, bad, GROUP_U3, date(2026, 12, 28))
        with self.assertRaises(writer.WriteRefused):                              # Tuesday has no availability
            writer.build_purchase(HDCW, LOOKUP, GROUP_U3, date(2026, 12, 29))

    def test_no_purchase_id_in_the_response_stops_and_never_deletes(self):
        result, _rest, gql, out = self.go(["booking"], gql=FakeGql(upsert_ids=()))
        self.assertNotIn("DeletePurchase", gql.ops())
        self.assertTrue(any("BY HAND" in line for line in out))


if __name__ == "__main__":
    unittest.main()
