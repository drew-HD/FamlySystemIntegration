"""Deposit pipeline: webhook, Slack approval, worker writes and guards, 50/25/25 schedule, backfill. Fakes only.

    python -m unittest tests.test_deposit_worker -q
"""

import os
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from actions.deposit import approval, backfill, reads, store, web, worker
from integrations import slack

LONDON = ZoneInfo("Europe/London")
DEAL, CHILD, BP, INST = "111", "child-1", "bp-1", "inst-city"
STAGES = {"s-pend": ("Operations", "Deposit Pending"), "s-paid": ("Operations", "Deposit Paid"),
          "s-left": ("Operations", "Left Nursery"), "s-sales": ("Sales", "Contract Signed")}


def deal(**over):
    d = {"dealstage": "s-pend", "pipeline": "ops", "famly_childid": CHILD, "famly_plan_status": "plan_created",
         "location_of_interest": "City", "deposit_amount": "1500", "deposit_preference": "Full Deposit Payment",
         "n1st_deposit_installment_50": "", "n2nd_and_3rd_deposit_installments": "",
         "child_first_name": "Ada", "child_last_name": "Test"}
    d.update(over)
    return d


class FakeHub:
    def __init__(self, d=None):
        self.deals = {DEAL: d or deal()}
        self.statuses = []

    def write_deposit_status(self, deal_id, value):
        self.statuses.append((deal_id, value))

    def get_deposit_deal(self, deal_id):
        return dict(self.deals[deal_id])

    def stage_names(self):
        return STAGES

    def search_deals(self, filters, properties, limit=100):
        ids = set(filters[0]["values"])
        return [{"id": k, **v} for k, v in self.deals.items() if v["dealstage"] in ids]


class FakeRest:
    def __init__(self, start="2027-03-01", deposits=None, drop_new=False):
        self.start, self.deposits, self.posts, self.drop_new = start, list(deposits or []), [], drop_new

    def get(self, path, params=None):
        if path.endswith("/summary"):
            return {"child": {"localStartDate": self.start}}
        if path.startswith("/v2/deposits/"):
            return {"deposits": [dict(d) for d in self.deposits]}
        raise AssertionError(path)

    def post(self, path, json_body=None, params=None):
        self.posts.append((path, json_body))
        if not self.drop_new:
            for r in json_body["deposits"]:
                self.deposits.append({"depositId": f"dep{len(self.posts)}", "amount": r["amount"], "date": r["date"],
                                      "status": "PAYED_EXTERNALLY" if r["status"] == "PAID_EXTERN" else "PENDING"})
        return {}


class FakeGql:
    def __init__(self, preview_total=None):
        self.calls, self.preview_total = [], preview_total

    persist_raises = False

    def execute(self, query_path, variables, operation_name):
        self.calls.append(variables)
        if variables["mode"] == "Persist" and self.persist_raises:
            raise TimeoutError("famly did not answer")
        total = self.preview_total
        if total is None:      # the unpaid deposits the fake rest holds are what an invoice would carry
            total = "0"
        return {"data": {"finance": {"invoices": {"create": {"invoices": [
            {"__typename": "InvoiceSuccess", "invoice": {"invoiceId": "inv1", "total": total}}]}}}}}

    def persists(self):
        return [c for c in self.calls if c["mode"] == "Persist"]


class FakeNotifier:
    def __init__(self, ref="C:1"):
        self.gates, self.notices, self.ref = [], [], ref

    def gate(self, text, job_id, nonce):
        self.gates.append((text, job_id, nonce))
        return self.ref

    def notice(self, text):
        self.notices.append(text)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DEPOSIT_STORE_PATH": str(Path(self.tmp.name) / "d.sqlite3"),
                                                "DEPOSIT_MODE": "live", "DEPOSIT_ALLOWED_DEAL_IDS": "*"})
        self.env.start()
        self.payer = mock.patch.object(reads, "the_bill_payer", return_value=(BP, None))
        self.payer.start()
        self.site = mock.patch.object(reads.derive, "site_for_location", return_value=("HDCITY", INST))
        self.site.start()
        self.hub, self.rest, self.gql, self.note = FakeHub(), FakeRest(), FakeGql(), FakeNotifier()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def run_at(self, y, m, d, hour=10):
        now = datetime(y, m, d, hour, tzinfo=LONDON)
        return worker.Worker(self.hub, self.rest, self.gql, None, self.note, now=now).run_once()

    def job(self):
        return store.jobs(store.ACTIVE + (store.DONE, store.DRY_DONE, store.REJECTED))[0]

    def approve(self):
        j = self.job()
        return approval.handle_click(slack.ACTION_DEPOSIT_APPROVE, f"{j.id}:{j.approval_nonce}", "U1")

    def to_approved(self, when=(2026, 11, 5)):
        store.enqueue(DEAL)
        self.run_at(*when)
        self.assertEqual(self.job().status, store.AWAITING_APPROVAL)
        self.assertEqual(self.approve(), approval.MSG_APPROVED)

    def preview_matches(self, amount):
        self.gql.preview_total = amount


class WebhookTests(Base):
    def test_webhook_queues_once_refuses_bad_ids_and_mode_off(self):
        self.assertEqual(web.handle({"dealId": "abc"})[1], 400)
        self.assertEqual(web.handle({"dealId": DEAL})[0]["outcome"], "queued")
        self.assertEqual(web.handle({"dealId": int(DEAL)})[0]["outcome"], "already_queued")
        with mock.patch.dict(os.environ, {"DEPOSIT_MODE": "off"}):
            self.assertEqual(web.handle({"dealId": "222"})[0]["outcome"], "refused_mode_off")


class WaitingTests(Base):
    def test_waits_for_child_and_plans_then_holds_after_the_wait(self):
        self.hub.deals[DEAL] = deal(famly_plan_status="")
        store.enqueue(DEAL)
        self.run_at(2026, 11, 5)
        self.assertEqual(self.job().status, store.WAITING)
        self.assertEqual(self.note.notices, [])
        self.run_at(2027, 1, 30)
        self.assertEqual((self.job().status, self.job().hold_code), (store.HELD, "waited_too_long"))

    def test_gaps_hold_without_a_gate(self):
        for over, code in (({"dealstage": "s-sales"}, "stage_not_eligible"),
                           ({"dealstage": "s-left"}, "stage_not_eligible"),
                           ({"deposit_amount": "1500.001"}, "deposit_amount_invalid"),
                           ({"deposit_preference": "50 / 25 / 25", "n1st_deposit_installment_50": "700",
                             "n2nd_and_3rd_deposit_installments": "400"}, "deposit_installments_mismatch")):
            with self.subTest(code=code):
                store._reset_for_tests()
                self.hub.deals[DEAL] = deal(**over)
                store.enqueue(DEAL)
                self.run_at(2026, 11, 5)
                self.assertEqual((self.job().status, self.job().hold_code), (store.HELD, code))
        self.assertEqual(self.note.gates, [])

    def test_an_existing_deposit_holds(self):
        self.rest.deposits = [{"depositId": "x", "amount": 1500, "date": "2026-01-01", "status": "PAYED_EXTERNALLY"}]
        store.enqueue(DEAL)
        self.run_at(2026, 11, 5)
        self.assertEqual(self.job().hold_code, "deposit_exists")

    def test_gate_shows_name_amounts_and_dry_note(self):
        with mock.patch.dict(os.environ, {"DEPOSIT_MODE": "dry"}):
            store.enqueue(DEAL)
            self.run_at(2026, 11, 5)
        text = self.note.gates[0][0]
        self.assertIn("Ada Test", text)
        self.assertIn("£1,500.00", text)
        self.assertIn("Dry run", text)


class ApprovalTests(Base):
    def test_clicks_are_atomic_and_nonce_checked(self):
        store.enqueue(DEAL)
        self.run_at(2026, 11, 5)
        j = self.job()
        self.assertEqual(approval.handle_click(slack.ACTION_DEPOSIT_APPROVE, f"{j.id}:wrong", "U"), approval.MSG_STALE)
        self.assertEqual(self.approve(), approval.MSG_APPROVED)
        self.assertEqual(approval.handle_click(slack.ACTION_DEPOSIT_APPROVE, f"{j.id}:{j.approval_nonce}", "U"),
                         approval.MSG_HANDLED)

    def test_reject_writes_nothing(self):
        store.enqueue(DEAL)
        self.run_at(2026, 11, 5)
        j = self.job()
        approval.handle_click(slack.ACTION_DEPOSIT_REJECT, f"{j.id}:{j.approval_nonce}", "U")
        self.run_at(2026, 11, 5)
        self.assertEqual((self.job().status, self.rest.posts), (store.REJECTED, []))


class FirstInstallmentTests(Base):
    def test_full_payment_adds_once_previews_then_persists(self):
        self.to_approved()
        self.preview_matches("1500.00")
        self.run_at(2026, 11, 6)
        self.assertEqual(len(self.rest.posts), 1)
        body = self.rest.posts[0][1]["deposits"][0]
        self.assertEqual((body["amount"], body["date"], body["status"]), (1500, "2026-11-06", ""))
        self.assertEqual([c["mode"] for c in self.gql.calls], ["Preview", "Persist"])
        self.assertTrue(self.gql.persists()[0]["notify"])
        # 6 Nov 2026 is a Friday: the period is Sunday 8 Nov only; due 7 days after the invoice date
        self.assertEqual([c["range"] for c in self.gql.calls], [{"from": "2026-11-08", "to": "2026-11-08"}] * 2)
        self.assertEqual(self.gql.persists()[0]["dueDate"], "2026-11-13")
        self.assertEqual(self.hub.statuses, [(DEAL, "deposit_added")])
        self.assertEqual(self.job().status, store.DONE)
        self.run_at(2026, 11, 7)
        self.assertEqual((len(self.rest.posts), len(self.gql.persists())), (1, 1))

    def test_dry_mode_and_not_allow_listed_write_nothing(self):
        with mock.patch.dict(os.environ, {"DEPOSIT_MODE": "dry"}):
            self.to_approved()
            self.run_at(2026, 11, 6)
        self.assertEqual((self.job().status, self.rest.posts, self.gql.calls), (store.DRY_DONE, [], []))
        store._reset_for_tests()
        with mock.patch.dict(os.environ, {"DEPOSIT_ALLOWED_DEAL_IDS": "999"}):
            self.to_approved()
            self.run_at(2026, 11, 6)
        self.assertEqual((self.job().hold_code, self.rest.posts), ("deal_not_allowed", []))
        self.assertEqual(self.hub.statuses, [])          # no HubSpot write outside live + allow-listed

    def test_a_hold_marks_the_deal_needs_attention(self):
        self.to_approved()
        self.preview_matches("1890.00")
        self.run_at(2026, 11, 6)
        self.assertEqual(self.hub.statuses, [(DEAL, "needs_attention")])

    def test_a_failed_status_write_does_not_block_famly(self):
        self.hub.write_deposit_status = mock.Mock(side_effect=RuntimeError("hubspot down"))
        self.to_approved()
        self.preview_matches("1500.00")
        self.run_at(2026, 11, 6)
        self.assertEqual((self.job().status, len(self.gql.persists())), (store.DONE, 1))

    def test_preview_mismatch_holds_without_persisting_and_resume_never_readds(self):
        self.to_approved()
        self.preview_matches("1890.00")                  # fees or a sibling on the same bill payer
        self.run_at(2026, 11, 6)
        self.assertEqual(self.job().hold_code, "invoice_preview_mismatch")
        self.assertEqual((len(self.rest.posts), self.gql.persists()), (1, []))
        store.resume(self.job().id)
        self.preview_matches("1500.00")
        self.run_at(2026, 11, 7)
        self.assertEqual((len(self.rest.posts), len(self.gql.persists()), self.job().status), (1, 1, store.DONE))

    def test_readback_mismatch_is_never_resent(self):
        self.to_approved()
        self.rest.drop_new = True
        self.run_at(2026, 11, 6)
        self.assertEqual(self.job().hold_code, "deposit_readback_mismatch")
        store.resume(self.job().id)
        self.run_at(2026, 11, 7)
        self.assertEqual((self.job().hold_code, len(self.rest.posts)), ("deposit_outcome_unknown", 1))

    def test_a_persist_that_fails_is_never_resent(self):
        self.to_approved()
        self.preview_matches("1500.00")
        self.gql.persist_raises = True
        self.run_at(2026, 11, 6)
        self.assertEqual(self.job().hold_code, "invoice_outcome_unknown")
        self.gql.persist_raises = False
        store.resume(self.job().id)
        self.run_at(2026, 11, 7)
        self.assertEqual((self.job().hold_code, len(self.gql.persists()), len(self.rest.posts)),
                         ("invoice_outcome_unknown", 1, 1))

    def test_switching_to_dry_after_a_deposit_was_added_holds_not_dry_done(self):
        self.to_approved()
        self.preview_matches("999.00")
        self.run_at(2026, 11, 6)                          # deposit added, invoice held
        store.resume(self.job().id)
        with mock.patch.dict(os.environ, {"DEPOSIT_MODE": "dry"}):
            self.run_at(2026, 11, 7)
        self.assertEqual((self.job().status, self.job().hold_code), (store.HELD, "deal_not_allowed"))

    def test_amounts_changed_after_approval_hold(self):
        self.to_approved()
        self.hub.deals[DEAL] = deal(deposit_amount="1600")
        self.run_at(2026, 11, 6)
        self.assertEqual((self.job().hold_code, self.rest.posts), ("plan_changed", []))

    def test_a_deposit_added_by_hand_before_the_write_holds(self):
        self.to_approved()
        self.rest.deposits = [{"depositId": "hand", "amount": 1500, "date": "2026-11-06", "status": "PENDING"}]
        self.run_at(2026, 11, 6)
        self.assertEqual((self.job().hold_code, self.rest.posts), ("deposit_exists", []))


class ScheduleTests(Base):
    def setUp(self):
        super().setUp()
        self.hub.deals[DEAL] = deal(deposit_preference="50 / 25 / 25", n1st_deposit_installment_50="750",
                                    n2nd_and_3rd_deposit_installments="375")
        self.rest.start = "2027-01-15"             # regular billing from January

    def test_split_end_to_end(self):
        self.to_approved((2026, 11, 5))           # installments 5 Nov, 1 Dec (own invoice), 1 Jan (covered)
        self.preview_matches("750.00")
        self.run_at(2026, 11, 5)
        self.assertEqual((len(self.rest.posts), len(self.gql.persists()), self.job().status),
                         (1, 1, store.SCHEDULED))
        self.run_at(2026, 11, 29)                 # too early for installment 2
        self.assertEqual(len(self.rest.posts), 1)
        self.run_at(2026, 11, 30)                 # the day before: added, not invoiced
        self.assertEqual(self.rest.posts[1][1]["deposits"][0], {"amount": 375, "date": "2026-12-01",
                                                                "note": "Deposit 2", "status": ""})
        self.assertEqual(len(self.gql.persists()), 1)
        self.preview_matches("375.00")
        self.run_at(2026, 12, 1, hour=7)          # before 08:00 London: no invoice yet
        self.assertEqual(len(self.gql.persists()), 1)
        self.run_at(2026, 12, 1, hour=9)
        self.assertEqual(len(self.gql.persists()), 2)
        self.run_at(2026, 12, 31)                 # installment 3 added
        self.run_at(2027, 1, 1, hour=9)           # January's regular run covers the child: no invoice
        self.assertEqual((len(self.rest.posts), len(self.gql.persists()), self.job().status), (3, 2, store.DONE))

    def test_a_late_approval_keeps_the_approved_dates_or_holds(self):
        self.to_approved((2026, 11, 5))           # shown: 5 Nov, 1 Dec, 1 Jan
        self.preview_matches("750.00")
        self.run_at(2026, 11, 7)                  # 2 days late: first dated 7 Nov, later dates unchanged
        self.assertEqual([i.on_date for i in store.installments(self.job().id)],
                         ["2026-11-07", "2026-12-01", "2027-01-01"])
        store._reset_for_tests()
        self.rest = FakeRest(start="2027-01-15")
        self.to_approved((2026, 11, 5))
        self.run_at(2026, 11, 20)                 # 1 Dec is now only 11 days away: not what was approved
        self.assertEqual((self.job().hold_code, self.rest.posts), ("plan_changed", []))

    def test_unreadable_start_on_invoice_day_holds(self):
        self.to_approved((2026, 11, 5))
        self.preview_matches("750.00")
        self.run_at(2026, 11, 5)
        self.run_at(2026, 11, 30)
        self.rest.start = ""
        self.run_at(2026, 12, 1, hour=9)
        self.assertEqual((self.job().hold_code, len(self.gql.persists())), ("start_date_missing", 1))

    def test_bill_payer_change_holds_a_later_installment(self):
        self.to_approved((2026, 11, 5))
        self.preview_matches("750.00")
        self.run_at(2026, 11, 5)
        reads.the_bill_payer.return_value = ("bp-2", None)
        self.run_at(2026, 11, 30)
        self.assertEqual((self.job().hold_code, len(self.rest.posts)), ("bill_payer_changed", 1))

    def test_start_moved_earlier_makes_the_invoice_covered(self):
        self.to_approved((2026, 11, 5))
        self.preview_matches("750.00")
        self.run_at(2026, 11, 5)
        self.run_at(2026, 11, 30)
        self.rest.start = "2026-12-10"            # now billed by December's regular run
        self.run_at(2026, 12, 1, hour=9)
        self.assertEqual(len(self.gql.persists()), 1)

    def test_start_moved_later_gets_its_own_invoice(self):
        self.to_approved((2026, 11, 5))
        self.preview_matches("750.00")
        self.run_at(2026, 11, 5)
        self.rest.start = "2027-04-01"            # January's run no longer covers installment 3
        self.preview_matches("375.00")
        for day in ((2026, 11, 30), (2026, 12, 1), (2026, 12, 31), (2027, 1, 1)):
            self.run_at(*day)
        self.assertEqual((len(self.gql.persists()), self.job().status), (3, store.DONE))


class CliWiringTests(unittest.TestCase):
    def test_the_commands_build_a_hub_that_can_write_the_status(self):
        from actions.deposit import cli
        for name in ("write_deposit_status", "get_deposit_deal", "stage_names", "search_deals"):
            self.assertTrue(hasattr(cli.HubSpotClient, name), name)


class BackfillTests(Base):
    def setUp(self):
        super().setUp()
        self.hub.deals = {"1": deal(dealstage="s-paid", famly_childid="c1"),
                          "2": deal(dealstage="s-paid", famly_childid="c2", deposit_preference="50 / 25 / 25"),
                          "3": deal(dealstage="s-pend", famly_childid="c3"),
                          "4": deal(dealstage="s-left", famly_childid="c4"),
                          "5": deal(dealstage="s-paid", famly_childid="")}
        self.cache = mock.patch.object(backfill, "_payers_by_child",
                                       return_value={"c1": [BP], "c2": [BP], "c3": [BP]})
        self.cache.start()
        self.out = Path(self.tmp.name) / "r.csv"

    def bf(self, confirm, **kw):
        return backfill.run(self.hub, self.rest, None, confirm=confirm, out_path=self.out,
                            today=date(2026, 11, 27), echo=lambda *_: None, **kw)

    def test_dry_run_writes_nothing(self):
        counts = self.bf(False)
        self.assertEqual(counts, {"would_add_paid": 1, "split_check_by_hand": 1, "pending_would_enqueue": 1,
                                  "child_missing": 1})
        self.assertEqual((self.rest.posts, store.jobs()), ([], []))

    def test_confirm_needs_live_mode(self):
        with mock.patch.dict(os.environ, {"DEPOSIT_MODE": "dry"}), self.assertRaises(backfill.Stop):
            self.bf(True)
        self.assertEqual(self.rest.posts, [])

    def test_confirm_adds_one_paid_deposit_and_queues_pending(self):
        counts = self.bf(True)
        self.assertEqual(counts["added_paid"], 1)
        self.assertEqual(self.rest.posts[0][1]["deposits"][0]["status"], "PAID_EXTERN")
        self.assertEqual(self.rest.posts[0][1]["deposits"][0]["date"], "2026-11-27")
        self.assertEqual(self.hub.statuses, [("1", "deposit_added")])
        self.assertEqual([j.deal_id for j in store.jobs()], ["3"])
        self.assertEqual(self.bf(True).get("deposit_exists"), 1)        # a second run never adds again
        self.assertEqual(len(self.rest.posts), 1)

    def test_confirm_writes_only_allow_listed_deals(self):
        with mock.patch.dict(os.environ, {"DEPOSIT_ALLOWED_DEAL_IDS": "999"}):
            counts = self.bf(True)
        self.assertEqual((counts.get("deal_not_allowed"), self.rest.posts), (1, []))

    def test_readback_mismatch_stops_the_run(self):
        self.rest.drop_new = True
        with self.assertRaises(backfill.Stop):
            self.bf(True)
        self.assertEqual(len(self.rest.posts), 1)


if __name__ == "__main__":
    unittest.main()
