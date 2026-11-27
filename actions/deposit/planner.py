"""Pure deposit planner: what deposits and invoices a deal needs, and when.

No I/O. Every failure is a HOLD reason code (`Hold`), never a guess: a wrong
amount or date charges a parent the wrong money.

Rules (Drew, 2026-11-27):
- `deposit_preference` "Full Deposit Payment": one deposit of `deposit_amount`
  dated the run day, invoiced at once.
- "50 / 25 / 25": three deposits. The first (`n1st_deposit_installment_50`) is
  dated the run day and invoiced at once. The second
  (`n2nd_and_3rd_deposit_installments`) is dated the 1st of next month if that
  is MORE than 14 days after the run day, else the 1st of the month after. The
  third (same amount) is dated the 1st of the month after the second.
- The three installments must add up to `deposit_amount` to the penny, and the
  first must be exactly half of it, else HOLD.
- An installment dated D needs its own invoice only if Famly's regular run on
  the 1st of D's month does not already invoice the child, i.e. the child's
  first plan starts in a LATER month than D (assumption, flagged to Drew:
  monthly billing in advance on the 1st covers a child starting any day of
  that month).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

FULL = "Full Deposit Payment"
SPLIT = "50 / 25 / 25"
MIN_DAYS_TO_SECOND = 14  # the 2nd installment's 1st-of-month must be MORE than this away

# Hold reason codes.
AMOUNT_MISSING = "deposit_amount_missing"
AMOUNT_INVALID = "deposit_amount_invalid"
PREFERENCE_MISSING = "deposit_preference_missing"
PREFERENCE_UNKNOWN = "deposit_preference_unknown"
INSTALLMENT_MISSING = "deposit_installment_missing"
INSTALLMENTS_MISMATCH = "deposit_installments_mismatch"
START_DATE_MISSING = "plan_start_missing"

PENNY = Decimal("0.01")


class Hold(Exception):
    def __init__(self, reason: str, **detail: Any):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Installment:
    number: int            # 1-based
    amount: Decimal        # pounds, 2 dp
    on: date               # deposit date
    invoice_on: date | None  # date to send its own invoice; None = covered by the regular run


def parse_amount(value: Any, code_missing: str = AMOUNT_MISSING) -> Decimal:
    """A HubSpot number property as pounds to the penny. Missing -> HOLD; negative,
    zero, non-numeric or more than 2 dp -> HOLD (never rounded)."""
    if value is None or str(value).strip() == "":
        raise Hold(code_missing)
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise Hold(AMOUNT_INVALID) from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(PENNY):
        raise Hold(AMOUNT_INVALID)
    return amount.quantize(PENNY)


def first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def second_installment_date(run_day: date) -> date:
    candidate = first_of_next_month(run_day)
    if (candidate - run_day).days > MIN_DAYS_TO_SECOND:
        return candidate
    return first_of_next_month(candidate)


def regular_run_covers(on: date, plan_start: date) -> bool:
    """True when Famly's regular invoice on the 1st of `on`'s month already invoices
    the child (the plan starts in that month or earlier)."""
    return (plan_start.year, plan_start.month) <= (on.year, on.month)


def plan(deal: dict, run_day: date, plan_start: date | None) -> list[Installment]:
    """The deposits for a deal. `plan_start` is the child's first plan start in Famly
    (needed only for the split's invoice rule)."""
    total = parse_amount(deal.get("deposit_amount"))
    pref = " ".join(str(deal.get("deposit_preference") or "").split())
    if not pref:
        raise Hold(PREFERENCE_MISSING)
    if pref == FULL:
        return [Installment(1, total, run_day, run_day)]
    if pref != " ".join(SPLIT.split()):
        raise Hold(PREFERENCE_UNKNOWN)

    first = parse_amount(deal.get("n1st_deposit_installment_50"), INSTALLMENT_MISSING)
    later = parse_amount(deal.get("n2nd_and_3rd_deposit_installments"), INSTALLMENT_MISSING)
    if first * 2 != total or first + later * 2 != total:
        raise Hold(INSTALLMENTS_MISMATCH, total=str(total), first=str(first), later=str(later))
    if plan_start is None:
        raise Hold(START_DATE_MISSING)

    second_on = second_installment_date(run_day)
    third_on = first_of_next_month(second_on)
    out = [Installment(1, first, run_day, run_day)]
    for number, on in ((2, second_on), (3, third_on)):
        out.append(Installment(number, later, on, None if regular_run_covers(on, plan_start) else on))
    return out


def total_of(installments: list[Installment]) -> Decimal:
    return sum((i.amount for i in installments), Decimal("0"))
