"""Deposit Famly calls and the deposit-probe guards. Fakes only.

    python -m unittest tests.test_deposit_famly -q
"""

import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

from actions.deposit import famly, probe

INST, CHILD, BP = "inst-1", "child-1", "bp-1"


class FakeRest:
    def __init__(self, test_child=True, replace=False):
        self.deposits = [{"depositId": "d0", "amount": 11, "date": "2026-11-30", "status": "PENDING"}]
        self.test_child, self.replace, self.posts = test_child, replace, []

    def get(self, path, params=None):
        if path.endswith("/summary"):
            return {"child": {"isTestChild": self.test_child}}
        if path.startswith("/v2/deposits/"):
            return {"deposits": list(self.deposits), "behaviors": []}
        if path == "/v2/invoicing/invoices":
            return []
        raise AssertionError(path)

    def post(self, path, json_body=None, params=None):
        self.posts.append((path, json_body))
        new = [{"depositId": f"n{len(self.posts)}", "amount": r["amount"], "date": r["date"],
                "status": r["status"] or "PENDING"} for r in json_body["deposits"]]
        self.deposits = new if self.replace else self.deposits + new
        return {}


class FakeGql:
    def __init__(self):
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append(variables)
        return {"data": {"finance": {"invoices": {"create": {"invoices": [
            {"__typename": "InvoiceSuccess", "invoice": {"invoiceId": "i1", "total": 1.5}}]}}}}}


class ClientTests(unittest.TestCase):
    def test_amounts_are_sent_as_pounds_and_never_rounded(self):
        self.assertEqual(famly.amount_json(Decimal("1500.00")), 1500)
        self.assertEqual(famly.amount_json(Decimal("375.50")), 375.5)
        with self.assertRaises(ValueError):
            famly.amount_json(Decimal("1.005"))

    def test_writer_refuses_unless_writes_are_allowed(self):
        rest, gql = FakeRest(), FakeGql()
        w = famly.DepositWriter(rest, gql, writes_allowed=False)
        with self.assertRaises(famly.WritesNotAllowed):
            w.add_deposits(INST, CHILD, BP, [{"amount": Decimal("1"), "date": "2026-11-27", "status": ""}])
        with self.assertRaises(famly.WritesNotAllowed):
            w.persist_invoice(BP, "2026-11-27", "2026-12-01", "2026-12-01", "2026-12-31", False)
        self.assertEqual((rest.posts, gql.calls), ([], []))

    def test_writer_refuses_missing_ids_and_bad_rows(self):
        w = famly.DepositWriter(FakeRest(), FakeGql(), writes_allowed=True)
        for args in ((INST, CHILD, "", [{"amount": Decimal("1"), "date": "d", "status": ""}]),
                     (INST, CHILD, BP, []),
                     (INST, CHILD, BP, [{"amount": Decimal("1"), "date": "d", "status": "PAID"}]),
                     (INST, CHILD, BP, [{"amount": Decimal("0"), "date": "d", "status": ""}])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                w.add_deposits(*args)

    def test_preview_is_never_persist_and_never_notifies(self):
        gql = FakeGql()
        famly.preview_invoice(gql, BP, "2026-11-27", "2026-12-01", "2026-12-01", "2026-12-31")
        self.assertEqual((gql.calls[0]["mode"], gql.calls[0]["notify"]), ("Preview", False))


class ProbeTests(unittest.TestCase):
    def run_probe(self, rest, steps, confirm=True):
        lines = []
        with mock.patch.object(probe.seed, "resolve_site", return_value=(INST, "g")), \
                mock.patch.object(probe, "_save"):
            probe.run(steps, confirm, "HDCW", CHILD, BP, rest, FakeGql(), out=lines.append,
                      today=date(2026, 11, 27))
        return "\n".join(lines)

    def test_writes_need_confirm_and_a_test_child(self):
        with self.assertRaises(probe.ProbeRefused):
            self.run_probe(FakeRest(), ["add"], confirm=False)
        rest = FakeRest(test_child=False)
        with self.assertRaises(probe.ProbeRefused):
            self.run_probe(rest, ["add"])
        self.assertEqual(rest.posts, [])

    def test_add_reports_whether_earlier_deposits_were_kept(self):
        self.assertIn("earlier deposits all kept = True", self.run_probe(FakeRest(), ["add"]))
        self.assertIn("earlier deposits all kept = False", self.run_probe(FakeRest(replace=True), ["add"]))


if __name__ == "__main__":
    unittest.main()
