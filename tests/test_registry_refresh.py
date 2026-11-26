"""registry-refresh (nightly): guards that stop before writing, drift flags, new-only Slack alerts, state kept on a
failed post, ids only in Slack and the CSV. Fakes only; temp registry, state and reports."""

import tempfile
from datetime import date
from pathlib import Path

from actions.registry import refresh, store
from tests.test_registry import TEST_NAME, Base, FakeHubSpot, child

SITES = {"inst-cw": "HDCW"}


class RefreshTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(self.tmp.name)
        self.posts = []
        self.post_ok = True

    def post(self, text):
        self.posts.append(text)
        return self.post_ok

    def run_refresh(self, children, deals=(), dry_run=False, day=date(2026, 11, 27)):
        return refresh.run(FakeHubSpot(list(deals)), None, dry_run=dry_run, post=self.post, today=day,
                           state_file=self.dir / "state.json", reports_dir=self.dir / "reports",
                           children={c.id: c for c in children}, links={}, site_codes=SITES)

    def seed(self, n):
        for i in range(n):
            store.upsert_child(f"r{i}", f"Testchild N{i}", "2024-01-02", "HDCW", "backfill")

    def test_new_children_and_links_are_added_and_the_first_run_is_a_baseline(self):
        result = self.run_refresh([child("c1", refs=["101"], urn="HD0001CW")], [{"id": "101", "dealstage": "ops"}])
        self.assertEqual(store.by_deal("101").famly_child_id, "c1")
        self.assertEqual(result["written"]["children_added"], 1)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("first run", self.posts[0])
        self.assertTrue((self.dir / "state.json").exists())

    def test_a_short_famly_roster_stops_before_writing(self):
        self.seed(20)
        kids = [child(f"r{i}") for i in range(10)]                     # half the registry: a partial read
        with self.assertRaises(refresh.RefreshAborted) as caught:
            self.run_refresh(kids + [child("new1")])
        self.assertEqual(caught.exception.code, "famly_roster_too_small")
        self.assertIsNone(store.by_child_id("new1"))
        self.assertIn("Nothing was written", self.posts[-1])
        self.assertFalse((self.dir / "state.json").exists())

    def test_one_site_coming_back_nearly_empty_stops(self):
        self.seed(20)
        for i in range(20):
            store.upsert_child(f"w{i}", "x", "2024-01-02", "HDWEST", "backfill")
        kids = [child(f"r{i}") for i in range(20)] + [child(f"n{i}") for i in range(20)]   # total fine, HDWEST gone
        with self.assertRaises(refresh.RefreshAborted) as caught:
            self.run_refresh(kids)
        self.assertEqual(caught.exception.code, "famly_site_roster_too_small")

    def test_a_read_failure_stops_and_says_so(self):
        class Broken(FakeHubSpot):
            def stage_names(self):
                raise TimeoutError()
        with self.assertRaises(refresh.RefreshAborted) as caught:
            refresh.run(Broken([]), None, post=self.post, state_file=self.dir / "s.json",
                        reports_dir=self.dir, children={"c1": child("c1")}, links={}, site_codes=SITES)
        self.assertEqual(caught.exception.code, "read_failed_TimeoutError")
        self.assertIsNone(store.by_child_id("c1"))

    def test_a_child_gone_from_famly_is_flagged_and_kept(self):
        self.seed(10)
        self.run_refresh([child(f"r{i}") for i in range(10)])           # baseline
        result = self.run_refresh([child(f"r{i}") for i in range(1, 10)])
        self.assertEqual(result["childrenGone"], 1)
        self.assertIsNotNone(store.by_child_id("r0"))                   # never deleted
        self.assertIn("no longer in Famly", self.posts[-1])
        self.assertIn("child r0", self.posts[-1])

    def test_only_new_issues_are_posted_and_a_quiet_night_posts_nothing(self):
        kids = [child("c1", urn="HD0001CW"), child("c2", name="Testchild Beta", urn="HD0001CW")]
        self.run_refresh(kids)                                         # baseline with a shared URN
        n = len(self.posts)
        self.run_refresh(kids)
        self.assertEqual(len(self.posts), n)                           # nothing new: silent
        kids.append(child("c3", name="Testchild Gamma"))
        result = self.run_refresh(kids, [{"id": "101", "dealstage": "ops", "famly_childid": "gone"}])
        self.assertEqual(result["new"], 1)
        self.assertIn("does not exist", self.posts[-1])
        self.assertNotIn("URN is on more than one", self.posts[-1])    # the old issue is not repeated

    def test_a_failed_post_keeps_the_state_so_the_issue_is_new_again(self):
        self.run_refresh([child("c1")])
        self.post_ok = False
        kids = [child("c1", urn="X1"), child("c2", name="Testchild Beta", urn="X1")]
        self.assertTrue(self.run_refresh(kids)["postFailed"])
        self.post_ok = True
        self.assertEqual(self.run_refresh(kids)["new"], 2)            # one per child on the shared URN

    def test_dry_run_writes_nothing_and_no_names_reach_slack_or_the_csv(self):
        result = self.run_refresh([child("c1", refs=["101"])], [{"id": "101", "dealstage": "ops"}], dry_run=True)
        self.assertIsNone(store.by_child_id("c1"))
        self.assertEqual(self.posts, [])
        self.assertIsNone(result["written"])
        self.seed(10)
        self.run_refresh([child(f"r{i}") for i in range(1, 10)])
        text = "\n".join(self.posts) + Path(result["report"]).read_text(encoding="utf-8")
        for word in TEST_NAME.split() + ["N0", "2024-01-02"]:
            self.assertNotIn(word, text)
