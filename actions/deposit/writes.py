"""The two deposit writes, each guarded so money is never doubled.

add_deposit(job, inst):
  1. the installment must be `pending`; the child's live deposit list must hold ONLY deposits this pipeline added
     (deposit_ids recorded for the child) and nothing with this amount and date; else HOLD deposit_list_changed.
  2. pending -> sending (atomic), then ONE POST of one unpaid deposit.
  3. read back: exactly one new row, same amount and date, PENDING -> added (deposit id kept);
     else HOLD deposit_readback_mismatch (left `sending`: never resent automatically).
  A job found `sending` (a crash mid-request) is HELD deposit_outcome_unknown.

send_invoice(job, inst, today):
  1. the deposit must be `added` and the invoice `due`.
  2. PREVIEW (nothing saved), period = the following Sunday only (from = to): exactly one invoice whose total equals the installment amount, else
     HOLD invoice_preview_mismatch (e.g. a sibling's or the child's fees would be on it).
  3. due -> sending (atomic), PERSIST with notify on -> sent; any failure -> HOLD invoice_outcome_unknown.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from . import config, famly, store

logger = logging.getLogger(__name__)


class Held(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _same(row: dict, inst: store.Inst) -> bool:
    try:
        return famly.amount_read(row.get("amount")) == inst.amount and row.get("date") == inst.on_date
    except Exception:  # noqa: BLE001 - an unreadable amount is not a match
        return False


def add_deposit(writer: famly.DepositWriter, rest, job: store.Job, inst: store.Inst) -> str:
    if inst.deposit_state == store.D_SENDING:
        raise Held("deposit_outcome_unknown")
    if inst.deposit_state != store.D_PENDING:
        return inst.deposit_id or ""
    if writer.writes_allowed is not True:
        raise Held("not_live")
    before = famly.list_deposits(rest, job.institution_id, job.child_id)
    ours = store.all_deposit_ids(job.child_id)
    if any(d.get("depositId") not in ours for d in before) or any(_same(d, inst) for d in before):
        raise Held("deposit_list_changed")
    if not store.inst_transition(job.id, inst.number, "deposit_state", store.D_PENDING, store.D_SENDING):
        raise Held("deposit_outcome_unknown")
    writer.add_deposits(job.institution_id, job.child_id, job.bill_payer_id,
                        [{"amount": inst.amount, "date": inst.on_date, "status": famly.STATUS_UNPAID,
                          "note": f"Deposit {inst.number}" if job.preference != "Full Deposit Payment" else "Deposit"}])
    after = famly.list_deposits(rest, job.institution_id, job.child_id)
    old = {d.get("depositId") for d in before}
    new = [d for d in after if d.get("depositId") not in old]
    if len(new) != 1 or not _same(new[0], inst) or new[0].get("status") != famly.READ_PENDING \
            or not old <= {d.get("depositId") for d in after}:
        raise Held("deposit_readback_mismatch")
    deposit_id = str(new[0]["depositId"])
    store.inst_transition(job.id, inst.number, "deposit_state", store.D_SENDING, store.D_ADDED, deposit_id=deposit_id)
    logger.info("DEPOSIT added job=%s child=%s installment=%s", job.id, job.child_id, inst.number)
    return deposit_id


def following_sunday(d: date) -> date:
    """The Sunday after `d` (a Sunday gives the next one). An invoice whose period is that one day carries no
    session fees, so it should hold only the deposit (Drew, 2026-11-27); the preview total still checks it."""
    return d + timedelta(days=(6 - d.weekday()) or 7)


def send_invoice(writer: famly.DepositWriter, gql, job: store.Job, inst: store.Inst, today: date) -> str:
    if inst.invoice_state == store.I_SENDING:
        raise Held("invoice_outcome_unknown")
    if inst.invoice_state != store.I_DUE:
        return inst.invoice_id or ""
    if inst.deposit_state != store.D_ADDED:
        raise Held("invoice_before_deposit")
    if writer.writes_allowed is not True:
        raise Held("not_live")
    invoice_date, due = today.isoformat(), (today + timedelta(days=config.invoice_due_days())).isoformat()
    range_from = range_to = following_sunday(today).isoformat()
    preview = famly.preview_invoice(gql, job.bill_payer_id, invoice_date, due, range_from, range_to)
    try:
        total_ok = len(preview) == 1 and preview[0]["ok"] and Decimal(str(preview[0]["total"])) == inst.amount
    except Exception:  # noqa: BLE001
        total_ok = False
    if not total_ok:
        raise Held("invoice_preview_mismatch")
    if not store.inst_transition(job.id, inst.number, "invoice_state", store.I_DUE, store.I_SENDING):
        raise Held("invoice_outcome_unknown")
    try:
        result = writer.persist_invoice(job.bill_payer_id, invoice_date, due, range_from, range_to, notify=True)
    except Exception as exc:  # noqa: BLE001 - outcome unknown: never resent automatically
        logger.error("DEPOSIT invoice job=%s failed: %s", job.id, type(exc).__name__)
        raise Held("invoice_outcome_unknown") from None
    if len(result) != 1 or not result[0]["ok"]:
        raise Held("invoice_outcome_unknown")
    invoice_id = str(result[0].get("invoiceId") or "")
    store.inst_transition(job.id, inst.number, "invoice_state", store.I_SENDING, store.I_SENT, invoice_id=invoice_id)
    logger.info("DEPOSIT invoice sent job=%s child=%s installment=%s", job.id, job.child_id, inst.number)
    return invoice_id
