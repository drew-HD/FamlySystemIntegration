"""deposit-probe: check the captured deposit and invoice requests on ONE verified test child.

Steps (each chosen with --step):
  read      list the child's deposits (amount, date, status per row), list the bill payer's invoices (count),
            and PREVIEW an invoice (mode Preview, nothing saved, nobody notified): total or errors.
  add       POST one 1.50 UNPAID deposit dated today, read back: are the earlier deposits all still there
            (add, not replace), is there exactly one new row, amount 1.50 (decimals kept), status PENDING?
  add-paid  POST one 1.00 PAID_EXTERN deposit dated today, read back: the status Famly stores for paid.
  invoice   PERSIST an invoice for the bill payer (notify false), then list invoices again.

Guards: writes need --confirm; the child's summary must say isTestChild true (re-read before each write);
at most MAX_WRITES writes. Deposits added here are removed by hand in Famly (no delete call is captured).
Raw responses are saved under reports/ (gitignored). Only ids, counts, amounts, dates and statuses printed.
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from actions.testchild_seed import runner as seed

from . import famly

STEPS = ("read", "add", "add-paid", "invoice")
WRITE_STEPS = {"add", "add-paid", "invoice"}
MAX_WRITES = 3
REPORTS = Path("reports")


class ProbeRefused(Exception):
    pass


def _rows(deposits: list[dict]) -> list[str]:
    return [f"  {d.get('depositId')}  {famly.amount_read(d.get('amount'))}  {d.get('date')}  {d.get('status')}"
            for d in deposits]


def _save(name: str, body) -> None:
    try:
        REPORTS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (REPORTS / f"deposit_probe_{name}_{stamp}.json").write_text(json.dumps(body, indent=2, default=str))
    except OSError:
        pass


def run(steps: list[str], confirm: bool, site: str, child_id: str, bill_payer_id: str, rest, gql,
        out: Callable[[str], None] = print, today: date | None = None) -> int:
    steps = list(dict.fromkeys(steps))
    if not steps or any(s not in STEPS for s in steps):
        raise ProbeRefused(f"choose --step from {', '.join(STEPS)}")
    if WRITE_STEPS & set(steps) and not confirm:
        raise ProbeRefused("write steps need --confirm")
    institution_id, _ = seed.resolve_site(site)
    today = today or date.today()
    writer = famly.DepositWriter(rest, gql, writes_allowed=True)
    writes = 0

    def verified() -> None:
        if not seed.is_test_child(rest.get(f"/v2/children/{child_id}/summary")):
            raise ProbeRefused("the child is not a verified test child (isTestChild); nothing written")

    verified()
    month_from = today.replace(day=1).isoformat()
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    month_to = date.fromordinal(nxt.toordinal() - 1).isoformat()

    for step in steps:
        before = famly.list_deposits(rest, institution_id, child_id)
        if step == "read":
            out(f"read: {len(before)} deposit(s)")
            for line in _rows(before):
                out(line)
            inv = famly.list_invoices(rest, institution_id, bill_payer_id)
            _save("invoices", inv)
            count = len(inv) if isinstance(inv, list) else len(inv.get("invoices", []) if isinstance(inv, dict) else [])
            out(f"read: {count} invoice(s) for the bill payer; top-level type {type(inv).__name__}"
                + (f", keys {sorted(inv)}" if isinstance(inv, dict) else ""))
            preview = famly.preview_invoice(gql, bill_payer_id, today.isoformat(), today.isoformat(),
                                            month_from, month_to)
            out(f"read: invoice PREVIEW {month_from}..{month_to}: {preview}")
            continue

        if writes >= MAX_WRITES:
            raise ProbeRefused(f"write cap ({MAX_WRITES}) reached")
        verified()
        writes += 1
        if step in ("add", "add-paid"):
            amount = Decimal("1.50") if step == "add" else Decimal("1.00")
            status = famly.STATUS_UNPAID if step == "add" else famly.STATUS_PAID
            resp = writer.add_deposits(institution_id, child_id, bill_payer_id,
                                       [{"amount": amount, "date": today.isoformat(), "status": status,
                                         "note": "probe"}])
            _save(step, resp)
            after = famly.list_deposits(rest, institution_id, child_id)
            old_ids = {d.get("depositId") for d in before}
            new = [d for d in after if d.get("depositId") not in old_ids]
            kept = old_ids <= {d.get("depositId") for d in after}
            out(f"{step}: before {len(before)}, after {len(after)}; earlier deposits all kept = {kept}; "
                f"new rows = {len(new)}")
            for line in _rows(new):
                out(line)
            if new:
                out(f"{step}: amount read back {famly.amount_read(new[0].get('amount'))} (sent {amount}), "
                    f"status read back {new[0].get('status')!r} (sent {status!r})")
        elif step == "invoice":
            result = writer.persist_invoice(bill_payer_id, today.isoformat(), today.isoformat(),
                                            month_from, month_to, notify=False)
            _save("invoice", result)
            out(f"invoice: PERSIST {month_from}..{month_to} notify=false: {result}")
    out(f"done: {writes} write(s)")
    return 0
