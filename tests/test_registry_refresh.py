"""registry-refresh (nightly): guards that stop before writing, drift flag, the full nightly issue list (names and
deal links, no Famly ids, new ones marked), closed deals left out, state kept on a failed post. Fakes only; temp
registry, state and reports."""

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
        self.posts = []
        return refresh.run(FakeHubSpot(list(deals)), None, dry_run=dry_run, post=self.post, today=day,
                           state_file=self.dir / "state.json", reports_dir=self.dir / "reports",
                           children={c.id: c for c in children}, links={}, site_codes=SITES)

    def seed(self, n):
        for i in range(n):
            store.upsert_child(f"r{i}", f"Testchild N{i}", "2024-01-02", "HDCW", "backfill")

    def test_new_children_and_links_are_added_and_a_clean_night_says_so(self):
        result = self.run_refresh([child("c1", refs=["101"], urn="HD0001CW")], [{"id": "101", "dealstage": "ops"}])
        self.assertEqual(store.by_deal("101").famly_child_id, "c1")
        self.assertEqual(result["written"]["children_added"], 1)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("no open issues", self.posts[0])
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

    def test_the_list_groups_by_issue_with_names_deal_links_and_no_famly_ids(self):
        store.upsert_child("gone1", "Testchild Gone", "2024-01-02", "HDCW", "backfill")
        store.add_deal("gone1", "301", store.OPERATIONS)
        kids = [child("c1", urn="HD0001CW"), child("c2", name="Testchild Beta", urn="HD0001CW")]
        deals = [{"id": "101", "dealstage": "ops", "famly_childid": "missing", "child_full_name": "Testchild Deal"},
                 {"id": "301", "dealstage": "ops"}]
        result = self.run_refresh(kids, deals)
        text = "\n".join(self.posts)
        self.assertEqual(result["openIssues"], 4)
        for heading, _ in (refresh.ISSUES[c] for c in ("recorded_child_not_in_famly", "urn_on_several_children",
                                                      refresh.CHILD_GONE)):
            self.assertIn(heading, text)
        for expected in ("• Testchild Deal: deal 101", "• Testchild Gone: deal 301", "• Testchild Alpha:",
                         "• Testchild Beta:", "How to fix"):
            self.assertIn(expected, text)
        for famly_id in ("c1", "c2", "gone1", "missing"):
            self.assertNotIn(famly_id, text)

    def test_issues_on_closed_deals_are_left_out(self):
        store.upsert_child("gone1", "Testchild Gone", "2024-01-02", "HDCW", "backfill")
        store.add_deal("gone1", "302", store.OPERATIONS)                # the gone child's only deal is closed
        deals = [{"id": "101", "dealstage": "left", "famly_childid": "missing"},
                 {"id": "102", "dealstage": "lost", "famly_childid": "missing2"},
                 {"id": "103", "dealstage": "done", "famly_childid": "missing3"},
                 {"id": "302", "dealstage": "left"},
                 {"id": "104", "dealstage": "ops", "famly_childid": "missing4"}]
        result = self.run_refresh([child("c1")], deals)
        self.assertEqual(result["openIssues"], 1)
        self.assertEqual(result["issuesOnClosedDeals"], 4)
        self.assertIn("deal 104", self.posts[0])
        self.assertNotIn("deal 101", self.posts[0])

    def test_the_full_list_is_posted_every_night_and_only_new_ones_are_marked(self):
        kids = [child("c1", urn="HD0001CW"), child("c2", name="Testchild Beta", urn="HD0001CW")]
        self.run_refresh(kids)                                         # first night: nothing marked new
        self.assertNotIn(refresh.NEW_MARK, self.posts[0])
        kids.append(child("c3", name="Testchild Gamma"))
        result = self.run_refresh(kids, [{"id": "101", "dealstage": "ops", "famly_childid": "gone"}])
        self.assertEqual(result["new"], 1)
        text = self.posts[0]
        self.assertIn("Testchild Beta", text)                          # the old issue is listed again
        new_lines = [line for line in text.splitlines() if line.startswith("•") and refresh.NEW_MARK in line]
        self.assertEqual(new_lines, [f"• Name not known: deal 101{refresh.NEW_MARK}"])

    def test_a_failed_post_keeps_the_state_so_the_issue_is_new_again(self):
        self.run_refresh([child("c1")])
        self.post_ok = False
        kids = [child("c1", urn="X1"), child("c2", name="Testchild Beta", urn="X1")]
        self.assertTrue(self.run_refresh(kids)["postFailed"])
        self.post_ok = True
        self.assertEqual(self.run_refresh(kids)["new"], 2)            # one per child on the shared URN

    def test_a_long_list_is_split_into_several_messages(self):
        kids = [child(f"c{i}", name=f"Testchild Number{i:03d}", urn="SHARED") for i in range(150)]
        self.run_refresh(kids)
        self.assertGreater(len(self.posts), 1)
        self.assertTrue(all(len(p) <= refresh.MAX_MESSAGE_CHARS for p in self.posts))
        self.assertEqual(sum(p.count("\n• ") for p in self.posts) + sum(p.startswith("• ") for p in self.posts),
                         150)

    def test_dry_run_writes_nothing_posts_nothing_and_returns_the_message(self):
        result = self.run_refresh([child("c1", refs=["101"])], [{"id": "101", "dealstage": "ops"}], dry_run=True)
        self.assertIsNone(store.by_child_id("c1"))
        self.assertEqual(self.posts, [])
        self.assertIsNone(result["written"])
        self.assertIn("no open issues", result["messages"][0])
        self.assertFalse((self.dir / "state.json").exists())
        self.assertIn("registry_refresh_dryrun_", result["report"])

    def test_the_csv_names_the_child_and_the_log_does_not(self):
        self.seed(10)
        self.run_refresh([child(f"r{i}") for i in range(10)])
        import logging
        prior = logging.root.manager.disable                           # other modules disable logging process-wide
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs(refresh.logger, level="INFO") as logs:
                result = self.run_refresh([child(f"r{i}") for i in range(1, 10)])
        finally:
            logging.disable(prior)
        csv_text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn(f"r0,,{TEST_NAME},", csv_text)                   # the name from the registry row
        self.assertIn(TEST_NAME, self.posts[0])
        for word in TEST_NAME.split() + ["2024-01-02"]:
            self.assertNotIn(word, "\n".join(logs.output))

    def test_a_registry_write_failure_says_rows_may_be_added(self):
        import sqlite3
        from unittest import mock
        with mock.patch.object(store, "add_deal", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(refresh.RefreshAborted) as caught:
                self.run_refresh([child("c1", refs=["101"])], [{"id": "101", "dealstage": "ops"}])
        self.assertEqual(caught.exception.code, "write_failed_OperationalError")
        self.assertIn("may already have been added", self.posts[-1])
