"""create-test-children: confirm gate, test-only payload guard, idempotence, caps. Fakes only."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.testchild_seed import runner


class FakePublic:
    def __init__(self, existing=None, reject=False):
        self.existing = existing or {}        # foreign id -> [child ids]
        self.reject = reject
        self.created = []
        self.ops = []

    def execute(self, query_path, variables, operation_name):
        self.ops.append(operation_name)
        if operation_name == runner.LOOKUP_OP:
            ids = self.existing.get(variables["foreignId"], [])
            return {"data": {"children": {"listByExternalSystemReference": {"result": [{"id": i} for i in ids]}}}}
        if self.reject:
            from core.client import GraphQLError
            raise GraphQLError("no", [{"message": "no"}])
        payload = variables["children"][0]
        self.created.append(payload)
        return {"data": {"children": {"create": [{"id": f"new-{payload['externalId']}"}]}}}


class FakeRest:
    def __init__(self, test_ids=None):
        self.test_ids = test_ids

    def get(self, path, params=None, **kw):
        child_id = path.split("/")[3]
        is_test = True if self.test_ids is None else child_id in self.test_ids
        return {"child": {"isTestChild": is_test}}


class SeedCase(unittest.TestCase):
    def go(self, public, rest=None, confirm=True):
        out = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "resolve_site", lambda code: (f"site-{code}", f"grp-{code}")):
            result = runner.run(confirm, rest or FakeRest(), public, out=out.append,
                                store_path=Path(tmp) / "t.json")
        return result, out


class SeedTests(SeedCase):
    def test_needs_confirm_and_writes_nothing(self):
        public = FakePublic()
        with self.assertRaises(runner.SeedRefused):
            self.go(public, confirm=False)
        self.assertEqual(public.ops, [])

    def test_one_test_child_per_site_all_flagged(self):
        public = FakePublic()
        result, _out = self.go(public)
        self.assertEqual(set(result.created), set(runner.SITES))
        self.assertEqual(len(public.created), 3)
        for payload in public.created:
            self.assertIs(payload["isTestChild"], True)
            self.assertTrue(payload["externalId"].startswith("AATEST-"))
            self.assertTrue(payload["name"].startswith("AATest"))

    def test_rerun_is_idempotent(self):
        public = FakePublic(existing={"AATEST-HDCW": ["c-cw"]})
        result, _out = self.go(public)
        self.assertEqual(result.existing, {"HDCW": "c-cw"})
        self.assertEqual(set(result.created), {"HDCITY", "HDWEST"})

    def test_existing_non_test_child_is_refused_not_touched(self):
        public = FakePublic(existing={"AATEST-HDCW": ["real"]})
        result, _out = self.go(public, rest=FakeRest(test_ids={"someone-else"}))
        self.assertEqual(result.skipped["HDCW"], "existing_not_test_child")
        self.assertNotIn("HDCW", result.created)

    def test_ambiguous_existing_is_skipped(self):
        public = FakePublic(existing={"AATEST-HDCITY": ["a", "b"]})
        result, _out = self.go(public)
        self.assertEqual(result.skipped["HDCITY"], "ambiguous_existing")

    def test_payload_guard_refuses_anything_not_a_test_child(self):
        good = runner.make_child_input("HDCW", "s", "g")
        runner.assert_test_payload(good)
        for bad in ({**good, "isTestChild": False}, {**good, "name": "Real Child"},
                    {**good, "externalId": "URN-1"}, {k: v for k, v in good.items() if k != "isTestChild"}):
            with self.assertRaises(runner.SeedRefused):
                runner.assert_test_payload(bad)

    def test_rejection_is_not_retried_and_cap_holds(self):
        public = FakePublic(reject=True)
        result, _out = self.go(public)
        self.assertEqual(result.writes, 3)                  # one attempt per site, no retries
        self.assertEqual(set(result.skipped.values()), {"create_rejected"})
        seeder = runner.Seeder(FakeRest(), FakePublic())
        seeder.result.writes = runner.MAX_CREATES
        with self.assertRaises(runner.SeedRefused):
            seeder.create(runner.make_child_input("HDCW", "s", "g"))

    def test_no_update_or_delete_document_in_package(self):
        text = (runner.HERE / "create_child.graphql").read_text().lower()
        self.assertNotIn("delete", text)
        self.assertNotIn("update", text)


if __name__ == "__main__":
    unittest.main()
