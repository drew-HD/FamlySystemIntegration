"""adhoc-probe-read: read-only guards, test-child verification, parsing. Fakes only."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from actions.adhoc_probe import runner

BODY = {"data": {"finance": {"revenueItems": {
    "productVersions": [],
    "sessionVersions": [
        {"title": "Morning", "type": "StandaloneSession", "validFrom": "2016-08-10",
         "pricing": [{"adHocPrice": "75.00", "period": "WEEKLY", "pricingGroupId": "pg1"}],
         "sessionAvailabilities": [{"day": "MONDAY", "sessionAvailabilityId": "sa-mon"},
                                   {"day": "TUESDAY", "sessionAvailabilityId": "sa-tue"}]}]}}}}


class FakeRest:
    def __init__(self, test_ids):
        self.test_ids = test_ids
        self.paths = []

    def get(self, path, params=None, **kw):
        self.paths.append(path)
        if path.endswith("/summary"):
            return {"isTestChild": path.split("/")[3] in self.test_ids, "pricingGroup": "x"}
        return []


class FakeGql:
    def __init__(self):
        self.docs = []

    def execute(self, query_path, variables, operation_name):
        self.docs.append(operation_name)
        if operation_name == runner.SESSIONS_OP:
            return BODY
        return {"data": {"finance": {"purchases": {"previewUpdate": {}}}}}


class ProbeTests(unittest.TestCase):
    def test_lookup_date_is_a_monday_at_least_30_days_out(self):
        d = runner.first_monday_after(date(2026, 11, 24))
        self.assertEqual(d.weekday(), 0)
        self.assertGreaterEqual((d - date(2026, 11, 24)).days, 30)

    def test_graphql_documents_are_queries_only(self):
        for path in (runner.SESSIONS_QUERY, runner.CONFLICTS_QUERY):
            self.assertNotIn("mutation", path.read_text().lower().replace("# ", ""))

    def test_only_verified_test_children_are_kept(self):
        kept = runner.verify_test_children(FakeRest({"a"}), {"HDCW": "a", "HDCITY": "b"})
        self.assertEqual(kept, {"HDCW": "a"})
        with self.assertRaises(runner.ProbeRefused):
            runner.verify_test_children(FakeRest(set()), {"HDCW": "b"})

    def test_parsing(self):
        self.assertIn("Morning", runner.describe_sessions(BODY)[0])
        self.assertEqual(runner.first_session_candidate(BODY)["sessionAvailabilityId"], "sa-mon")
        self.assertEqual(runner.pricing_fields({"a": {"pricingGroupId": "g"}}), ['a.pricingGroupId = "g"'])

    def test_end_to_end_reads_only_and_saves_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "t.json"
            store.write_text(json.dumps({"HDCW": "a"}))
            rest, gql, out = FakeRest({"a"}), FakeGql(), []
            result = runner.run_reads(rest, gql, out.append, today=date(2026, 11, 24), store_path=store,
                                      report_dir=Path(tmp))
            self.assertEqual(gql.docs, [runner.SESSIONS_OP, runner.CONFLICTS_OP, runner.CONFLICTS_OP])
            self.assertTrue(Path(result["report"]).exists())
            self.assertTrue(all(p.startswith("/v2/") for p in rest.paths))


if __name__ == "__main__":
    unittest.main()
