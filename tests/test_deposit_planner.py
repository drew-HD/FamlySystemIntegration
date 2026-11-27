"""Deposit planner: amounts, installment dates and the invoice rule. Pure, no I/O.

    python -m unittest tests.test_deposit_planner -q
"""

import unittest
from datetime import date
from decimal import Decimal

from actions.deposit import planner as p


def split_deal(total="1500", first="750", later="375", pref="50 / 25 / 25"):
    return {"deposit_amount": total, "deposit_preference": pref,
            "n1st_deposit_installment_50": first, "n2nd_and_3rd_deposit_installments": later}


class AmountTests(unittest.TestCase):
    def test_full_payment_is_one_deposit_invoiced_today(self):
        out = p.plan({"deposit_amount": "1500", "deposit_preference": "Full Deposit Payment"},
                     date(2026, 11, 27), None)
        self.assertEqual(out, [p.Installment(1, Decimal("1500.00"), date(2026, 11, 27), date(2026, 11, 27))])

    def test_bad_amounts_hold_and_are_never_rounded(self):
        for value, code in (("", p.AMOUNT_MISSING), (None, p.AMOUNT_MISSING), ("abc", p.AMOUNT_INVALID),
                            ("0", p.AMOUNT_INVALID), ("-10", p.AMOUNT_INVALID), ("1500.005", p.AMOUNT_INVALID),
                            ("NaN", p.AMOUNT_INVALID), ("Infinity", p.AMOUNT_INVALID)):
            with self.subTest(value=value), self.assertRaises(p.Hold) as cm:
                p.plan({"deposit_amount": value, "deposit_preference": "Full Deposit Payment"},
                       date(2026, 11, 27), None)
            self.assertEqual(cm.exception.reason, code)

    def test_preference_missing_or_unknown_holds(self):
        for pref, code in (("", p.PREFERENCE_MISSING), ("Monthly", p.PREFERENCE_UNKNOWN)):
            with self.subTest(pref=pref), self.assertRaises(p.Hold) as cm:
                p.plan({"deposit_amount": "1500", "deposit_preference": pref}, date(2026, 11, 27), None)
            self.assertEqual(cm.exception.reason, code)

    def test_split_amounts_must_add_up_to_the_penny(self):
        out = p.plan(split_deal(), date(2026, 11, 10), date(2027, 6, 1))
        self.assertEqual([i.amount for i in out], [Decimal("750.00"), Decimal("375.00"), Decimal("375.00")])
        self.assertEqual(p.total_of(out), Decimal("1500.00"))
        for bad in (split_deal(first="700"), split_deal(later="374.99"), split_deal(total="1500.01")):
            with self.subTest(bad=bad), self.assertRaises(p.Hold) as cm:
                p.plan(bad, date(2026, 11, 10), date(2027, 6, 1))
            self.assertEqual(cm.exception.reason, p.INSTALLMENTS_MISMATCH)

    def test_split_installment_missing_holds(self):
        with self.assertRaises(p.Hold) as cm:
            p.plan(split_deal(later=""), date(2026, 11, 10), date(2027, 6, 1))
        self.assertEqual(cm.exception.reason, p.INSTALLMENT_MISSING)

    def test_split_without_a_plan_start_holds(self):
        with self.assertRaises(p.Hold) as cm:
            p.plan(split_deal(), date(2026, 11, 10), None)
        self.assertEqual(cm.exception.reason, p.START_DATE_MISSING)


class DateTests(unittest.TestCase):
    def test_second_installment_needs_more_than_14_days(self):
        self.assertEqual(p.second_installment_date(date(2026, 11, 16)), date(2026, 12, 1))   # 15 days
        self.assertEqual(p.second_installment_date(date(2026, 11, 17)), date(2027, 1, 1))    # exactly 14
        self.assertEqual(p.second_installment_date(date(2026, 11, 30)), date(2027, 1, 1))

    def test_dates_cross_the_year_end(self):
        out = p.plan(split_deal(), date(2026, 12, 20), date(2027, 9, 1))
        self.assertEqual([i.on for i in out], [date(2026, 12, 20), date(2027, 2, 1), date(2027, 3, 1)])
        out = p.plan(split_deal(), date(2026, 11, 5), date(2027, 9, 1))
        self.assertEqual([i.on for i in out], [date(2026, 11, 5), date(2026, 12, 1), date(2027, 1, 1)])


class InvoiceRuleTests(unittest.TestCase):
    def test_later_installments_get_invoices_only_before_the_child_is_billed(self):
        run = date(2026, 11, 5)                        # installments 5 Nov, 1 Dec, 1 Jan
        cases = {
            date(2027, 3, 1): [run, date(2026, 12, 1), date(2027, 1, 1)],   # starts later: all invoiced
            date(2027, 1, 15): [run, date(2026, 12, 1), None],              # Jan run covers the 3rd
            date(2026, 12, 1): [run, None, None],                           # Dec run covers both
            date(2026, 12, 31): [run, None, None],
        }
        for start, expected in cases.items():
            with self.subTest(start=start):
                self.assertEqual([i.invoice_on for i in p.plan(split_deal(), run, start)], expected)


if __name__ == "__main__":
    unittest.main()
