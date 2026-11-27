"""The plan approval message (Drew, 2026-11-27): title, facts, sessions without prices, Pricing, Other periods,
Warnings, Approve / Reject only, dd/mm/yyyy dates. Fakes only."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actions.read_child_plans.runner import parse_plan
from integrations import plan_facts, slack
from tests import fast_sqlite  # noqa: F401

CHILD = "child-1"


def plan():
    return parse_plan({
        "childId": CHILD, "from": "2026-09-01", "to": "2027-08-01",
        "planParts": [{"planPartId": "pp", "sessionBookings": [{"sessionId": "s-1", "day": "MONDAY"}]}],
        "planStates": [
            {"from": "2026-09-01", "to": "2027-03-01", "monthlyEstimate": 812.5,
             "planPartStates": [{"weeklyTotal": 187.5}]},
            {"from": "2027-03-01", "to": "2027-08-01", "monthlyEstimate": 650.0,
             "planPartStates": [{"weeklyTotal": 150.0}]},
        ],
    })


class FakeHub:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value, error, []

    def read_deal_property(self, deal_id, name):
        self.calls.append((deal_id, name))
        if self.error:
            raise self.error
        return self.value


class LayoutTests(unittest.TestCase):
    def text(self, **facts):
        base = {"index": 1, "total": 4, "termTimeOnly": True, "fees": "Meals only (no educational activities and "
                "extras)", "funding": "30", "advance": ("2026-09-01", "2027-07-31"), "room": "Hatchlings"}
        base.update(facts)
        rounding = [SimpleNamespace(from_="2026-09-01", estimate=812.5, target=813.0)]
        return slack.build_summary(plan(), [], session_titles={"s-1": "Full Day"}, child_name="Alex Smith",
                                   site={"label": "City"}, rounding=rounding, facts=base,
                                   today=__import__("datetime").date(2026, 10, 1))

    def test_the_message_reads_top_to_bottom(self):
        self.assertEqual(self.text().split("\n\n")[0].splitlines(), [
            "*Alex Smith plan 2 of 4 approval* (from 01/09/2026 to 31/07/2027)",
            "• Site: City · Room: Hatchlings",
            "• Termly · Fees: Meals only (no educational activities and extras)",
            "• Funding: 30 hrs",
            "• Advance fee: 09/2026 to 07/2027",
        ])
        text = self.text()
        self.assertIn("*Sessions* (1)\n• Monday — Full Day\n", text)
        self.assertIn("*Pricing*\n• Weekly total: 187.50\n• Monthly estimate: 812.50\n• Rounded: 813.00", text)
        self.assertIn("*Other periods*\n• Price changes from 01/03/2027 (usually when the child turns 3)\n"
                      "   Monthly estimate: 650.00", text)
        self.assertTrue(text.endswith("*Warnings*\n• None."))

    def test_a_rounding_of_only_the_later_period_is_never_shown_as_the_current_one(self):
        rounding = [SimpleNamespace(from_="2027-03-01", estimate=650.0, target=649.0)]
        text = slack.build_summary(plan(), [], rounding=rounding, facts={},
                                   today=__import__("datetime").date(2026, 10, 1))
        pricing = text.split("*Pricing*\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("Rounded", pricing)
        self.assertIn("Monthly estimate: 650.00 · Rounded: 649.00", text)

    def test_an_unfunded_full_year_plan_without_advance_fee(self):
        head = self.text(termTimeOnly=False, funding=None, advance=None, index=0, total=1).split("\n\n")[0]
        self.assertIn("plan 1 of 1 approval", head)
        self.assertIn("• Full year · Fees:", head)
        self.assertNotIn("Funding", head)
        self.assertNotIn("Advance", head)

    def test_the_buttons_are_approve_and_reject_and_the_preview_id_is_not_shown(self):
        blocks = slack._approval_blocks("text", "pid-123")
        self.assertEqual([b["type"] for b in blocks], ["section", "actions"])
        self.assertEqual([e["action_id"] for e in blocks[1]["elements"]], [slack.ACTION_APPROVE, slack.ACTION_REJECT])
        self.assertNotIn("pid-123", json.dumps(blocks[0]))


class FactTests(unittest.TestCase):
    def test_the_fee_option_values_map_to_their_meaning(self):
        cases = {"Full Fees": "Full fees", "Term only": "Fully funded",
                 "Meals Only": "Activities only (no meals)",
                 "Enrichment Extras Only": "Meals only (no educational activities and extras)",
                 "No Additional Fees": "No extras", "All Year Round": "All Year Round", "": None, None: None}
        for value, label in cases.items():
            with self.subTest(value=value):
                self.assertEqual(plan_facts.fee_label(value), label)

    def test_the_fee_option_is_read_by_deal_id_and_a_failure_is_unknown(self):
        hub = FakeHub("Term only")
        self.assertEqual(plan_facts.fee_option("123", hub), "Fully funded")
        self.assertEqual(hub.calls, [("123", "full_funded")])
        self.assertIsNone(plan_facts.fee_option("123", FakeHub(error=RuntimeError("down"))))
        self.assertIsNone(plan_facts.fee_option(None, FakeHub("Full Fees")))

    def test_the_room_comes_from_the_child_summary_and_the_groups_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "groups.json"
            path.write_text(json.dumps({"institutions": {"HDCITY": {"groups": {"g-1": "Hatchlings"}}}}))
            rest = SimpleNamespace(get=lambda p: {"child": {"groupId": "g-1"}})
            with mock.patch.dict(os.environ, {"GROUPS_CATALOGUE_FILE": str(path)}):
                self.assertEqual(plan_facts.room(CHILD, "HDCITY", rest), "Hatchlings")
                self.assertIsNone(plan_facts.room(CHILD, "HDWEST", rest))
        broken = SimpleNamespace(get=mock.Mock(side_effect=RuntimeError("down")))
        self.assertIsNone(plan_facts.room(CHILD, "HDCITY", broken))

    def test_facts_from_a_funded_term_time_payload(self):
        facts = plan_facts.facts_for({"funded": "true", "funding_entitlement": "15", "termTimeOnly": "true"},
                                     CHILD, "HDCITY", deal_id="9", index=2, total=3, hubspot=FakeHub("Full Fees"),
                                     rest=SimpleNamespace(get=lambda p: {}))
        self.assertEqual((facts["funding"], facts["termTimeOnly"], facts["fees"], facts["index"], facts["total"]),
                         ("15", True, "Full fees", 2, 3))
        unfunded = plan_facts.facts_for({"funded": "false", "funding_entitlement": "30"}, CHILD, "HDCITY",
                                        hubspot=FakeHub(), rest=SimpleNamespace(get=lambda p: {}))
        self.assertIsNone(unfunded["funding"])

    def test_dates_in_slack_are_dd_mm_yyyy(self):
        from datetime import date
        self.assertEqual(slack.fmt_date("2026-01-15"), "15/01/2026")
        self.assertEqual(slack.fmt_date(date(2026, 1, 5)), "05/01/2026")
        self.assertEqual(slack.fmt_month("2026-09-01"), "09/2026")
        self.assertEqual(slack.fmt_date("soon"), "soon")
        self.assertIsNone(slack.fmt_date(None))


if __name__ == "__main__":
    unittest.main()
