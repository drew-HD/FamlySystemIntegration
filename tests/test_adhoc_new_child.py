"""Ad hoc step 3b: create a NEW child (contacts, bill payer, URN), then book and tag. Fakes only; nothing is live.
Money and write safety: one create, never a replay; settle by reads; the booking is exactly the approved one."""

import logging
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

from actions.adhoc import approval, child_input, deal_writeback, messages, new_child, store, worker
from actions.adhoc import writes as writes_mod
from actions.adhoc_plan import planner, urn_plan
from actions.onboard import famly_writes
from actions.pull_roster.store import ChildRow
from actions.onboard.bill_payer_writes import LinkResult, StageResult
from actions.onboard_dry_run import bill_payers as payers_mod
from actions.onboard_dry_run.links import DealContact
from actions.onboard_audit.runner import Index
from integrations import slack
from tests.test_adhoc_plan import CW, LOOKUP, TODAY, Contact, deal, kid
from tests.test_adhoc_worker import Case, FakeNotifier, FakeWrites, plan

NAMES = ("Zed", "Quill", "Mum", "Parent", "Dad")
URN = "HD0042CW"


def contacts():
    mk = lambda hid, first, last, email, payer: DealContact(  # noqa: E731
        hid, email, payer, props={"firstname": first, "lastname": last}, has_first_and_last_name=True)
    return [mk("h2", "Dad", "Parent", "dad@x.org", False), mk("h1", "Mum", "Parent", "mum@x.org", True)]


def new_plan():
    p = plan(child=())
    p.update(firstDay="2026-12-28", groupId="grp-1", groupTitle="Littlewings", adHocOnly=True,
             contacts=[{"hubspotContactId": "h1", "isBillPayer": True}, {"hubspotContactId": "h2", "isBillPayer": False}],
             billPayer={"status": "would_create", "flag": None, "billPayerId": None, "matches": 0,
                        "recipient": "will_add"},
             urn={"status": "allocated", "urn": URN}, steps=["create_child_no_end_date", "create_contacts",
                                                              "assign_ad_hoc_only_tag", "book_1_sessions"])
    p["purchase"]["childId"] = None
    return p


class FakeFamly:
    """Famly reads. `children` is what the deal's allocated URN finds (an ad hoc child carries no deal reference);
    a create adds the new child to it."""

    def __init__(self, children=(), settle_error=False):
        self.children, self.settle_error = list(children), settle_error
        self.contacts = {}                       # hubspot id -> famly contact id

    script = None                                # optional: call number -> list (or raises) for the roster scan
    calls = 0
    created = False

    def children_by_urn(self, urn):
        raise AssertionError("FamlyReads.children_by_urn memoises its roster: the create path must scan a fresh roster")

    def children_by_deal_id(self, deal_id):
        raise AssertionError("an ad hoc child carries no deal reference: nothing may look it up by one")

    def bill_payers(self, site_id): return []

    def locate_contact(self, child_id, hid, emails):
        return SimpleNamespace(ids=[self.contacts[hid]] if hid in self.contacts else [])


class FakeDealWriter:
    """Stands in for deal_writeback.DealWriter; records calls and their order (shared `events`)."""

    def __init__(self, events, urn_fail=False, childid_code=deal_writeback.CHILDID_WRITTEN):
        self.events, self.urn_fail, self.childid_code, self.calls = events, urn_fail, childid_code, []

    def ensure_urn(self, deal_id, urn):
        self.calls.append(("urn", deal_id, urn))
        self.events.append("hubspot:urn")
        if self.urn_fail:
            raise deal_writeback.WritebackFailed(deal_writeback.URN_WRITE_FAILED)
        return "written"

    def ensure_child_id(self, deal_id, child_id):
        self.calls.append(("child_id", deal_id, child_id))
        self.events.append("hubspot:child_id")
        return self.childid_code


class NewChildCase(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ADHOC_MODE": "live"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.famly, self.sent, self.fail_contacts = FakeFamly(), [], set()
        self.events = []                                    # the order of Famly and HubSpot writes
        self.deal_writer = FakeDealWriter(self.events)
        self.writes = FakeWrites()
        roster = mock.patch("actions.urn_lookup.runner.fetch_roster", side_effect=self.fake_fetch_roster)
        roster.start()
        self.addCleanup(roster.stop)
        self.create_child_effect = None
        for target, side in (("create_child", self.fake_create_child), ("create_contact", self.fake_create_contact)):
            p = mock.patch.object(famly_writes, target, side_effect=side)
            p.start()
            self.addCleanup(p.stop)
        g = mock.patch("actions.onboard_dry_run.derive.group_id_for",
                       side_effect=lambda site, title: "grp-1" if title == "Littlewings" else "grp-other")
        g.start()
        self.addCleanup(g.stop)
        self.stage = mock.patch.object(new_child.bill_payer_writes, "run_stage",
                                       return_value=StageResult("bill_payer_created", "bp1"))
        self.run_stage = self.stage.start()
        self.addCleanup(self.stage.stop)
        link = mock.patch.object(new_child.bill_payer_writes, "link_recipient",
                                 return_value=LinkResult("bill_payer_recipient_linked", True))
        self.link = link.start()
        self.addCleanup(link.stop)

    def fake_fetch_roster(self, client=None):
        """A FRESH roster scan each call: `famly.children` is whatever carries the URN right now."""
        self.famly.calls += 1
        ids = self.famly.script(self.famly.calls) if self.famly.script else list(self.famly.children)
        return [ChildRow(id=i, external_id=URN) for i in ids]

    def fake_create_child(self, client, payload):
        self.events.append("famly:child")
        self.sent.append(("child", payload))
        if self.create_child_effect == "rejected":
            raise famly_writes.CreateRejected("child_rejected")
        if self.create_child_effect == "unknown":
            raise famly_writes.OutcomeUnknown()
        self.famly.children, self.famly.created = ["new-1"], True
        return "new-1"

    def fake_create_contact(self, client, payload):
        email = payload["email"]
        self.sent.append(("contact", payload))
        hid = payload["externalSystemsReferences"][0]["foreignId"]
        if hid in self.fail_contacts:
            raise famly_writes.CreateRejected("contact_rejected")
        self.famly.contacts[hid] = f"fc-{hid}"
        return f"fc-{hid}"

    def deps(self, plans):
        d = super().deps(plans)
        d.writes, d.public, d.deal_writer = self.writes, object(), self.deal_writer
        d.famly_factory = lambda: self.famly
        d.deal_context = lambda deal_id: (deal(id=deal_id), contacts())
        d.sleep = lambda s: None
        return d

    def go(self, p=None, answer_deps=None):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(p or new_plan())
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        return store.latest_for_deal("1"), deps

    def kinds(self):
        return [k for k, _ in self.sent]


class WriteOrderTests(NewChildCase):
    def test_happy_path_creates_once_then_books_the_approved_purchase_and_tags(self):
        job, _ = self.go()
        self.assertEqual((job.status, job.last_error_code, job.child_id), (store.DONE, None, "new-1"))
        self.assertEqual(self.kinds(), ["child", "contact", "contact"])           # one child, then the contacts
        payloads = [p for _, p in self.sent[1:]]
        self.assertEqual([p["externalSystemsReferences"][0]["foreignId"] for p in payloads], ["h1", "h2"])  # payer first
        approved = new_plan()["purchase"]
        booked = [c for c in self.writes.calls if c[0] == "book"]
        self.assertEqual(len(booked), 1)
        self.assertEqual(booked[0][2], {**approved, "childId": "new-1"})            # approved payload, childId filled
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag"])          # no end-date step
        self.assertEqual(self.run_stage.call_count, 1)
        self.assertEqual(job.outcomes[:4], ["hubspot:childid_written", "contact:h1:created", "contact:h2:created",
                                            "billpayer:bill_payer_created"])

    def test_the_child_payload_has_no_last_day_and_carries_the_group_and_urn(self):
        self.go()
        payload = self.sent[0][1]
        self.assertEqual(payload["sitesRelation"], [{"siteId": "inst-cw", "firstDay": "2026-12-28"}])
        self.assertEqual((payload["currentGroupId"], payload["externalId"]), ("grp-1", URN))
        self.assertNotIn("isTestChild", payload)
        self.assertEqual(payload["externalSystemsReferences"], [{"foreignId": URN, "system": "URN"}])   # no HUBSPOT deal id

    def test_no_names_reach_slack_outcomes_or_logs(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        root, level = logging.getLogger(), logging.getLogger().level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(level)))
        job, _ = self.go()
        everything = " ".join(m["text"] for m in self.notifier.sent) + str(job.outcomes) + str(job.plan) + " ".join(records)
        for word in NAMES:
            self.assertNotIn(word, everything)
        gate = self.notifier.sent[0]["text"]
        for line in ("Child doesn't exist in Famly, to be created", "no end date", "Contacts to create: 2",
                     "a new bill payer will be created", f"URN: {URN}", "Littlewings"):
            self.assertIn(line, gate)

    def test_a_contact_failure_is_independent_and_the_booking_still_happens(self):
        self.fail_contacts = {"h1"}                                                  # the bill payer contact is refused
        job, _ = self.go()
        self.assertEqual(self.kinds(), ["child", "contact", "contact"])               # the second was still tried
        self.assertIn("contact:h1:contact_rejected", job.outcomes)
        self.assertIn("contact:h2:created", job.outcomes)
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag"])
        self.assertEqual(job.status, store.NEEDS_REVIEW)
        self.assertIn("contact:h1:contact_rejected", job.last_error_code)
        self.assertIn("contact:h1:contact_rejected", self.notifier.sent[-1]["text"])

    def test_an_unconfirmed_bill_payer_ends_needs_review_and_is_never_sent_twice(self):
        self.run_stage.return_value = StageResult("bill_payer_outcome_unknown", None, sent=True)
        job, _ = self.go()
        self.assertEqual(self.run_stage.call_count, 1)
        self.assertEqual((job.status, "bill_payer_outcome_unknown" in job.last_error_code), (store.NEEDS_REVIEW, True))
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag"])            # the booking still ran
        self.assertEqual(self.link.call_count, 0)                                        # no link without a confirmed payer


class SafetyTests(NewChildCase):
    def test_child_create_sent_blocks_a_replay(self):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        job = store.latest_for_deal("1")
        self.assertTrue(store.mark_child_create_sent(job.id))
        self.assertFalse(store.mark_child_create_sent(job.id))
        worker.run_once(deps)
        after = store.latest_for_deal("1")
        self.assertEqual((after.status, after.last_error_code), (store.NEEDS_REVIEW, "duplicate_create_blocked"))
        self.assertEqual((self.sent, self.writes.calls), ([], []))

    def test_settle_matrix_after_a_create_that_did_not_answer(self):
        cases = [
            # effect, children found by the reference afterwards, readable, status, code
            ("rejected", [], True, store.FAILED, "child_rejected"),
            ("unknown", [], True, store.NEEDS_REVIEW, "child_outcome_unknown"),
            ("rejected", [], False, store.NEEDS_REVIEW, "child_outcome_unknown"),
            ("unknown", [], False, store.NEEDS_REVIEW, "child_outcome_unknown"),
            ("rejected", ["a", "b"], True, store.NEEDS_REVIEW, "child_ambiguous"),
        ]
        for effect, found, readable, status, code in cases:
            with self.subTest(effect=effect, found=found, readable=readable):
                store._reset_for_tests()
                self.sent.clear()
                self.create_child_effect = effect

                def script(n, found=found, readable=readable):
                    if n == 1:
                        return []                                  # the pre-create lookup: nothing there
                    if not readable:
                        raise RuntimeError("down")                 # the settle read fails
                    return list(found)
                self.famly = FakeFamly()
                self.famly.script = script
                job, _ = self.go()
                self.assertEqual((job.status, job.last_error_code), (status, code))
                self.assertEqual(self.kinds(), ["child"])                       # one create, no contact, no replay
                self.assertEqual(self.writes.calls, [])                          # no booking

    def test_a_create_that_errors_but_landed_is_settled_by_the_lookup_and_continues(self):
        self.create_child_effect = "unknown"
        self.famly.children = []
        real = self.fake_create_child

        def landed(client, payload):                                       # the request landed, the answer was lost
            self.famly.children = ["new-1"]
            return real(client, payload)
        famly_writes.create_child.side_effect = landed
        job, _ = self.go()
        self.assertEqual((job.child_id, self.kinds()[0], self.kinds().count("child")), ("new-1", "child", 1))
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag"])

    def test_a_child_that_appears_before_the_write_stops_everything(self):
        for found in ({"children": ["x"]}, {"children": ["x", "y"]}):
            with self.subTest(found=found):
                store._reset_for_tests()
                self.sent.clear()
                self.famly = FakeFamly(**found)
                job, _ = self.go()
                self.assertEqual((job.status, job.last_error_code), (store.NEEDS_REVIEW, "plan_changed"))
                self.assertEqual((self.sent, self.writes.calls), ([], []))

    def test_no_urn_in_the_plan_means_no_child_is_created(self):
        plan = new_plan()
        plan["urn"] = {"status": "allocated"}                       # a plan with no URN: never create without it
        store.enqueue("1", "live")
        deps = self.deps(plan)
        deps.writes = self.writes
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        job = store.latest_for_deal("1")
        self.assertEqual((job.status, job.last_error_code, self.sent, self.writes.calls),
                         (store.NEEDS_REVIEW, "urn_missing", [], []))

    def test_a_dob_change_that_moves_the_group_creates_nothing(self):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        deps.deal_context = lambda deal_id: (deal(child_date_of_birth="2025-06-01"), contacts())   # now Hatchlings
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        job = store.latest_for_deal("1")
        self.assertEqual((job.status, job.last_error_code), (store.NEEDS_REVIEW, "plan_changed"))
        self.assertEqual((self.sent, self.writes.calls), ([], []))

    def test_a_name_only_change_is_created_with_the_planned_data_from_the_same_read(self):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        deps.deal_context = lambda deal_id: (deal(child_first_name="Zedd"), contacts())    # one read, used as is
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        self.assertEqual(store.latest_for_deal("1").status, store.DONE)
        payload = self.sent[0][1]
        self.assertEqual((payload["currentGroupId"], payload["birthday"]), ("grp-1", "2024-06-01"))

    def test_the_write_phase_reuses_the_plan_read_and_never_rereads_the_deal(self):
        from actions.adhoc_plan.readers import Readers
        r = Readers.__new__(Readers)
        r._context = {"1": ("deal", "contacts")}
        self.assertEqual(r.deal_and_contacts("1"), ("deal", "contacts"))
        self.assertEqual(r.deal_and_contacts("1"), (None, None))        # nothing to re-read: the job stops

    def test_changed_contacts_since_approval_write_nothing(self):
        self.writes = FakeWrites()
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        deps.deal_context = lambda deal_id: (deal(), contacts()[:1])
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        self.assertEqual((store.latest_for_deal("1").last_error_code, self.sent), ("plan_changed", []))

    def test_a_crash_after_the_create_is_needs_review_and_a_retry_books_the_found_child_without_a_second_create(self):
        self.fail_contacts = {"h2"}                                                   # one parent was not created
        self.writes = FakeWrites()
        self.writes.book = mock.Mock(side_effect=RuntimeError("boom"))               # the worker dies before booking
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        crashed = store.latest_for_deal("1")
        self.assertEqual((crashed.status, crashed.last_error_code, crashed.child_id),
                         (store.NEEDS_REVIEW, "worker_error_after_write", "new-1"))
        self.assertEqual(self.kinds().count("child"), 1)
        # a Retry is a fresh job; the child now exists, so the planner matches it and no create is possible
        self.writes = FakeWrites()
        retry = plan(child=("new-1",))
        retry["match"]["via"] = "external_system_reference"
        store.enqueue("1", "live")
        deps2 = self.deps(retry)
        worker.run_once(deps2)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps2)
        job = store.latest_for_deal("1")
        self.assertEqual(self.kinds().count("child"), 1)                              # still one create
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag", "end"])
        self.assertEqual((job.status, "contacts_missing" in job.last_error_code), (store.NEEDS_REVIEW, True))

    def test_recover_moves_a_writing_new_child_job_to_needs_review_with_its_child_id(self):
        store.enqueue("1", "live")
        job = store.claim_next()
        store.update(job.id, child_id="new-1")
        store.set_status(job.id, store.WRITING)
        self.assertEqual(store.recover_in_flight(), 1)
        after = store.get(job.id)
        self.assertEqual((after.status, after.last_error_code, after.child_id), (store.NEEDS_REVIEW, "interrupted", "new-1"))

    def test_booking_rejected_after_the_child_exists_is_failed_and_lists_earlier_problems(self):
        self.fail_contacts = {"h2"}
        self.writes = FakeWrites(book=writes_mod.BookingRejected("booking_rejected"))
        store.enqueue("1", "live")
        deps = self.deps(new_plan())
        deps.writes = self.writes
        worker.run_once(deps)
        approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
        worker.run_once(deps)
        job = store.latest_for_deal("1")
        self.assertEqual(job.status, store.FAILED)
        self.assertIn("contact:h2:contact_rejected", job.last_error_code)
        self.assertEqual(self.kinds().count("child"), 1)


class ModeAndHoldTests(NewChildCase):
    def test_new_child_not_built_is_gone_and_dry_mode_still_asks_and_writes_nothing(self):
        self.assertFalse(hasattr(worker, "NEW_CHILD_NOT_BUILT"))
        self.assertNotIn("new_child_not_built", messages.SENTENCES)
        with mock.patch.dict(os.environ, {"ADHOC_MODE": "dry"}):
            store.enqueue("1", "dry")
            deps = self.deps(new_plan())
            deps.writes = mock.Mock(side_effect=AssertionError("dry mode wrote"))
            worker.run_once(deps)
            approval.handle_click(slack.ACTION_ADHOC_APPROVE, self.notifier.value(), "u")
            worker.run_once(deps)
        self.assertEqual((store.latest_for_deal("1").status, self.sent), (store.DRY_DONE, []))

    def test_live_mode_asks_for_approval_for_a_new_child(self):
        store.enqueue("1", "live")
        worker.run_once(self.deps(new_plan()))
        job = store.latest_for_deal("1")
        self.assertEqual((job.status, self.notifier.sent[-1]["kind"]), (store.AWAITING_APPROVAL, "gate"))


class ChildInputTests(unittest.TestCase):
    def test_payload_shape_and_inputs_are_checked(self):
        d = deal(id="9")
        out = child_input.build_adhoc_child_input(d, "9", "inst", "grp", date(2026, 12, 28), URN)
        self.assertEqual(set(out), {"name", "birthday", "currentGroupId", "records", "sensitiveRecords", "sitesRelation",
                                    "externalId", "externalSystemsReferences"})
        self.assertNotIn("lastDay", out["sitesRelation"][0])
        with self.assertRaises(Exception):
            child_input.build_adhoc_child_input(d, "9", "inst", "", date(2026, 12, 28), URN)
        with self.assertRaises(Exception):
            child_input.build_adhoc_child_input(d, "9", "inst", "grp", date(2026, 12, 28), "")


class PlannerNewChildTests(unittest.TestCase):
    def build(self, cs=None, payers=(), urn=None, group="grp-1", **deal_over):
        deps = planner.Deps(has_plan=lambda c: False, read_sessions=lambda c, d: LOOKUP, preview=lambda c, p, d: {},
                            price_child_for_site=lambda s: "t",
                            urn=urn or (lambda deal_id, site: {"status": "would_allocate"}),
                            bill_payers=lambda site_id: list(payers))
        with mock.patch.object(planner.derive, "group_id_for",
                               side_effect=(lambda s, t: group) if group else planner.derive.Hold("group_missing")) as g:
            out = planner.build_plan(deal(**deal_over), "Approved", contacts() if cs is None else cs,
                                     Index({}), deps, TODAY)
        return out, g

    def test_group_is_by_age_at_the_first_booked_day_and_the_section_is_complete(self):
        out, g = self.build(child_date_of_birth="2024-12-28")        # exactly 24 months on 2026-12-28
        self.assertEqual(out["holds"], [])
        self.assertEqual((out["firstDay"], out["groupId"], out["groupTitle"]), ("2026-12-28", "grp-1", "Littlewings"))
        self.assertEqual(g.call_args.args[1], "Littlewings")
        self.assertEqual(out["contacts"][0], {"hubspotContactId": "h1", "isBillPayer": True})
        self.assertEqual(out["billPayer"]["status"], "would_create")
        self.assertEqual(out["urn"], {"status": "would_allocate"})
        out2, g2 = self.build(child_date_of_birth="2024-12-29")      # one day short of 24 months: Hatchlings
        self.assertEqual(g2.call_args.args[1], "Hatchlings")

    def test_holds_never_guess(self):
        self.assertIn("group_missing", self.build(group=None)[0]["holds"])
        self.assertIn("bill_payer_missing", self.build(cs=[DealContact("h2", "d@x.org", False, props={"firstname": "a", "lastname": "b"},
                                                         has_first_and_last_name=True)])[0]["holds"])
        no_email = [DealContact("h1", "", True, props={"firstname": "a", "lastname": "b"}, has_first_and_last_name=True)]
        self.assertIn("contact_email_missing", self.build(cs=no_email)[0]["holds"])
        self.assertIn("birthday_missing", self.build(child_date_of_birth="")[0]["holds"])
        off = self.build(urn=lambda d, s: {"status": "generation_off"})[0]
        self.assertIn("urn_generation_off", off["holds"])
        self.assertNotIn("urn", self.build(group=None)[0])          # a number is never reserved for a held plan


class UrnAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"ONBOARD_STORE_PATH": os.path.join(self.tmp.name, "o.sqlite3"),
                                               "ONBOARD_URN_GENERATION": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)
        from actions.onboard.urn_alloc import UrnService
        hubspot = SimpleNamespace(search_deals=lambda filters, props, limit=100: (
            [{"child_urn": "HD0007CW"}] if filters[0]["operator"] == "HAS_PROPERTY" else []))
        famly = SimpleNamespace(urn_roster=lambda: [SimpleNamespace(external_id="HD0010CW")],
                                children_by_urn=lambda urn: [])
        self.service = UrnService(hubspot, famly)

    def test_numbers_come_from_the_shared_allocator_and_are_never_duplicated(self):
        a = urn_plan.urn_section(self.service, "1", "HDCW", True)
        b = urn_plan.urn_section(self.service, "2", "HDCW", True)
        again = urn_plan.urn_section(self.service, "1", "HDCW", True)
        self.assertEqual((a["urn"], b["urn"], again["urn"]), ("HD0011CW", "HD0012CW", "HD0011CW"))
        # an onboarding deal asking next gets the following number from the SAME table
        self.assertEqual(self.service.allocate("onboard-deal", None, "HDCW"), "HD0013CW")

    def test_dry_mode_reserves_nothing_and_the_off_switch_and_unknown_site_hold(self):
        self.assertEqual(urn_plan.urn_section(self.service, "1", "HDCW", False), {"status": "would_allocate"})
        self.assertEqual(urn_plan.urn_section(self.service, "1", "HDCW", True)["urn"], "HD0011CW")   # still the first
        self.assertEqual(urn_plan.urn_section(self.service, "1", "NOPE", True), {"status": "site_unknown"})
        with mock.patch.dict(os.environ, {"ONBOARD_URN_GENERATION": "off"}):
            self.assertEqual(urn_plan.urn_section(self.service, "3", "HDCW", True), {"status": "generation_off"})

    def test_a_failing_read_reserves_nothing(self):
        bad = SimpleNamespace(search_deals=mock.Mock(side_effect=RuntimeError("x")))
        from actions.onboard.urn_alloc import UrnService
        self.assertEqual(urn_plan.urn_section(UrnService(bad, self.service._famly), "9", "HDCW", True),
                         {"status": "unavailable"})



class WritebackAndRosterTests(NewChildCase):
    """2026-11-25: the first live new-child job could not read its child back (a memoised roster), and Drew wants the
    URN and the Famly child id on the HubSpot deal."""

    def test_the_urn_goes_on_the_deal_before_the_create_and_the_child_id_after(self):
        job, _ = self.go()
        self.assertEqual(self.events[:3], ["hubspot:urn", "famly:child", "hubspot:child_id"])
        self.assertEqual(self.deal_writer.calls, [("urn", "1", URN), ("child_id", "1", "new-1")])
        self.assertEqual(job.status, store.DONE)

    def test_a_failed_urn_write_stops_before_any_famly_request(self):
        self.deal_writer = FakeDealWriter(self.events, urn_fail=True)
        job, _ = self.go()
        self.assertEqual((job.status, job.last_error_code), (store.NEEDS_REVIEW, "urn_hubspot_write_failed"))
        self.assertEqual((self.sent, self.writes.calls, self.famly.calls), ([], [], 0))     # nothing sent, not even a scan

    def test_a_failed_child_id_write_never_blocks_famly_the_booking_still_happens(self):
        self.deal_writer = FakeDealWriter(self.events, childid_code=deal_writeback.CHILDID_WRITE_FAILED)
        job, _ = self.go()
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag"])
        self.assertEqual((job.status, job.last_error_code), (store.NEEDS_REVIEW, "hubspot_childid_write_failed"))
        self.assertIn("hubspot_childid_write_failed", self.notifier.sent[-1]["text"])

    def test_the_read_back_sees_a_child_created_after_the_first_scan(self):
        # the regression: the pre-create scan finds nothing, the create adds the child, the read-back must find it
        job, _ = self.go()
        self.assertEqual((job.status, job.child_id), (store.DONE, "new-1"))
        self.assertGreaterEqual(self.famly.calls, 2)                    # a fresh scan before AND after the create

    def test_existing_children_scans_a_fresh_roster_on_every_call(self):
        rosters = iter([[], [ChildRow(id="c9", external_id=URN)]])
        with mock.patch("actions.urn_lookup.runner.fetch_roster", side_effect=lambda client=None: next(rosters)):
            self.assertEqual(new_child.existing_children(object(), URN), [])
            self.assertEqual(new_child.existing_children(object(), URN), ["c9"])


class ResumeContactsTests(NewChildCase):
    """A child an earlier job made but never reached the contacts for: a fresh job creates them (lookup first)."""

    def earlier_job(self, outcomes=None):
        job = store.enqueue("1", "live").job
        store.mark_child_create_sent(job.id)
        store.update(job.id, child_id="c-old", outcomes=outcomes)
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code="readback_child_lookup_failed")

    def resume_plan(self):
        p = new_plan()
        p["match"] = {"via": "allocated_urn", "childIds": ["c-old"], "urn": URN}
        p["resumeContacts"] = True
        p["purchase"]["childId"] = "c-old"
        p["steps"] = ["create_contacts", "assign_ad_hoc_only_tag", "remove_end_date_if_set", "book_1_sessions"]
        return p

    def test_contacts_are_created_for_the_existing_child_booked_and_the_child_id_recorded(self):
        self.earlier_job(outcomes=None)
        job, _ = self.go(self.resume_plan())
        self.assertEqual(self.kinds(), ["contact", "contact"])               # no second child
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag", "end"])
        self.assertEqual(self.deal_writer.calls, [("urn", "1", URN), ("child_id", "1", "c-old")])  # both onto the deal
        self.assertEqual((job.status, job.child_id), (store.DONE, "c-old"))

    def test_a_failed_urn_write_on_a_resume_is_a_problem_code_and_the_job_carries_on(self):
        self.earlier_job()
        self.deal_writer = FakeDealWriter(self.events, urn_fail=True)
        job, _ = self.go(self.resume_plan())
        self.assertEqual([c[0] for c in self.writes.calls], ["book", "tag", "end"])     # the child exists: still booked
        self.assertEqual((job.status, job.last_error_code), (store.NEEDS_REVIEW, "urn_hubspot_write_failed"))

    def test_a_parent_already_on_the_child_is_looked_up_first_and_not_created_again(self):
        self.earlier_job(outcomes=["hubspot:childid_written"])               # stopped before any contact outcome
        self.famly.contacts["h1"] = "fc-existing"                            # the bill payer is already there
        job, _ = self.go(self.resume_plan())
        self.assertEqual([p["externalSystemsReferences"][0]["foreignId"] for k, p in self.sent if k == "contact"], ["h2"])
        self.assertIn("contact:h1:present", job.outcomes)

    def test_no_resume_when_the_earlier_job_recorded_contact_outcomes(self):
        from actions.adhoc_plan import readers
        self.earlier_job(outcomes=["contact:h1:created", "contact:h2:readback_failed"])
        self.assertFalse(readers.resume_contacts_for_deal("1", "c-old"))
        self.assertFalse(readers.resume_contacts_for_deal("1", "another-child"))
        self.assertFalse(readers.resume_contacts_for_deal("2", "c-old"))

    def test_resume_is_detected_for_a_child_with_no_contact_outcomes(self):
        from actions.adhoc_plan import readers
        self.earlier_job(outcomes=None)
        self.assertTrue(readers.resume_contacts_for_deal("1", "c-old"))

    def test_changed_contacts_since_approval_write_nothing_on_a_resume(self):
        self.earlier_job()
        p = self.resume_plan()
        p["contacts"] = p["contacts"][:1]                                    # the plan had one contact, the deal has two
        job, _ = self.go(p)
        self.assertEqual((job.status, job.last_error_code, self.sent, self.writes.calls),
                         (store.NEEDS_REVIEW, "plan_changed", [], []))

    def test_a_resume_is_rechecked_at_write_time_and_stops_if_contacts_were_recorded_since(self):
        self.earlier_job(outcomes=["contact:h1:created"])             # recorded after the plan was approved
        job, _ = self.go(self.resume_plan())
        self.assertEqual((job.status, job.last_error_code, self.sent, self.writes.calls),
                         (store.NEEDS_REVIEW, "plan_changed", [], []))

    def test_the_gate_message_says_contacts_will_be_created_for_the_existing_child(self):
        self.earlier_job()
        self.go(self.resume_plan())
        gate = next(m for m in self.notifier.sent if m["kind"] == "gate")
        self.assertIn("Parent contacts: 2 to be created for this child", gate["text"])


class DealWriterTests(unittest.TestCase):
    """deal_writeback.DealWriter: only two properties, never overwrites, always confirmed."""

    class Client:
        def __init__(self, props=None, fail_write=False, swallow=False, fail_read=False):
            self.props, self.fail_write, self.swallow, self.fail_read, self.writes = dict(props or {}), fail_write, swallow, fail_read, []

        def get_deal(self, deal_id):
            if self.fail_read:
                raise deal_writeback.HubSpotError("down")
            return dict(self.props)

        def write_deal_properties(self, deal_id, properties):
            self.writes.append(dict(properties))
            if self.fail_write:
                raise deal_writeback.HubSpotError("boom")
            if not self.swallow:
                self.props.update(properties)

    def test_the_real_client_classes_are_checked_at_start_up(self):
        from core.hubspot import HubSpotClient, HubSpotDealWriter
        self.assertTrue(callable(getattr(HubSpotDealWriter, "write_deal_properties", None)))
        self.assertTrue(callable(getattr(HubSpotDealWriter, "get_deal", None)))
        with self.assertRaises(TypeError):                         # the plain read client cannot write: caught early
            deal_writeback.DealWriter(object.__new__(HubSpotClient))

    def test_urn_is_written_when_empty_confirmed_and_left_alone_when_equal(self):
        c = self.Client()
        self.assertEqual(deal_writeback.DealWriter(c).ensure_urn("1", URN), "written")
        self.assertEqual(c.writes, [{"child_urn": URN}])
        self.assertEqual(deal_writeback.DealWriter(c).ensure_urn("1", URN.lower()), "present")   # no second write
        self.assertEqual(len(c.writes), 1)

    def test_a_different_urn_is_never_overwritten_and_an_unconfirmed_write_fails(self):
        for client in (self.Client({"child_urn": "HD0001WM"}), self.Client(swallow=True), self.Client(fail_read=True)):
            with self.assertRaises(deal_writeback.WritebackFailed) as caught:
                deal_writeback.DealWriter(client).ensure_urn("1", URN)
            self.assertEqual(caught.exception.code, "urn_hubspot_write_failed")
        self.assertEqual(self.Client({"child_urn": "HD0001WM"}).writes, [])
        with self.assertRaises(deal_writeback.WritebackFailed):
            deal_writeback.DealWriter(self.Client()).ensure_urn("1", "")

    def test_a_write_that_errors_but_landed_counts_as_written(self):
        class Lands(self.Client):
            def write_deal_properties(self, deal_id, properties):
                self.props.update(properties)
                raise deal_writeback.HubSpotError("5xx after it ran")
        self.assertEqual(deal_writeback.DealWriter(Lands()).ensure_urn("1", URN), "written")
        self.assertEqual(deal_writeback.DealWriter(self.Client(fail_write=True)).ensure_child_id("1", "c1"),
                         deal_writeback.CHILDID_WRITE_FAILED)

    def test_child_id_codes_and_the_two_property_guard(self):
        c = self.Client()
        self.assertEqual(deal_writeback.DealWriter(c).ensure_child_id("1", "c1"), deal_writeback.CHILDID_WRITTEN)
        self.assertEqual(deal_writeback.DealWriter(c).ensure_child_id("1", "c1"), deal_writeback.CHILDID_PRESENT)
        self.assertEqual(deal_writeback.DealWriter(c).ensure_child_id("1", "c2"), deal_writeback.CHILDID_WRITE_FAILED)  # never overwrites
        self.assertEqual(c.props["famly_childid"], "c1")
        self.assertEqual(deal_writeback.ALLOWED, {"child_urn", "famly_childid"})
        with self.assertRaises(AssertionError):
            deal_writeback.DealWriter(c)._ensure("1", "end_date", "2026-01-01", lambda a, b: a == b)

if __name__ == "__main__":
    unittest.main()
