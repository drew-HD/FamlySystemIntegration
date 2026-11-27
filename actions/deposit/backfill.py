"""deposit-backfill: record the deposits of existing Operations deals in Famly. Dry run unless --confirm.

Per deal (Operations pipeline, from a HubSpot search):
  Deposit Paid and later stages   one deposit of `deposit_amount`, marked PAID_EXTERN (paid outside Famly),
                                  dated the run day, note "Deposit (backfill)". Paid deposits are never invoiced.
  Deposit Pending                 not written here: --confirm queues the deal for the normal deposit pipeline
                                  (Slack approval, invoice, installments).
  Left Nursery and other stages   skipped.
Skipped with a code (in the CSV): child_missing, preference "50 / 25 / 25" (split_check_by_hand: how much is
paid so far is not on the deal), every planner amount code, site_unmapped, bill_payer_missing/ambiguous,
deposit_exists (any deposit already on the child). A read-back that does not show exactly the new paid deposit
STOPS the run. --confirm also needs DEPOSIT_MODE=live. The CSV (reports/, gitignored) holds deal and child ids,
stage, amount and the outcome; no names.
"""

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from actions.onboard_dry_run import bill_payers as payer_scan
from actions.onboard_dry_run import derive
from core.hubspot import DEPOSIT_DEAL_PROPERTIES

from . import config, famly, planner, store


class Stop(Exception):
    pass


def _payers_by_child(public, institution_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in payer_scan.scan_site(public, institution_id):
        for child_id in p.children:
            if payer_scan.bills_the_child(p, child_id):
                out.setdefault(child_id, []).append(p.id)
    return out


def run(hub, rest, public, *, confirm: bool, limit: int | None = None, out_path: Path | None = None,
        today: date | None = None, echo: Callable[[str], None] = print) -> dict:
    if confirm and config.mode() != config.MODE_LIVE:
        raise Stop("--confirm needs DEPOSIT_MODE=live")
    today = today or date.today()
    stages = hub.stage_names()
    wanted = {sid: labels for sid, labels in stages.items()
              if derive.deposit_state(*labels) in (derive.DEPOSIT_PAID, derive.DEPOSIT_UNPAID)}
    deals = hub.search_deals([{"propertyName": "dealstage", "operator": "IN", "values": sorted(wanted)}],
                             list(DEPOSIT_DEAL_PROPERTIES))
    writer = famly.DepositWriter(rest, None, writes_allowed=confirm)
    payer_cache: dict[str, dict] = {}
    rows, counts, written = [], {}, 0

    def note(deal, child_id, stage, amount, outcome):
        rows.append({"deal_id": deal["id"], "child_id": child_id or "", "stage": stage, "amount": amount or "",
                     "preference": deal.get("deposit_preference") or "", "outcome": outcome})
        counts[outcome] = counts.get(outcome, 0) + 1

    for deal in sorted(deals, key=lambda d: d["id"]):
        pipeline, stage = wanted[str(deal.get("dealstage"))]
        state = derive.deposit_state(pipeline, stage)
        child_id = str(deal.get("famly_childid") or "").strip()
        if state == derive.DEPOSIT_UNPAID:
            if confirm:
                result, _job = store.enqueue(deal["id"], source="backfill")
                note(deal, child_id, stage, deal.get("deposit_amount"), f"pending_{result}")
            else:
                note(deal, child_id, stage, deal.get("deposit_amount"), "pending_would_enqueue")
            continue
        if not child_id:
            note(deal, "", stage, deal.get("deposit_amount"), "child_missing")
            continue
        pref = " ".join(str(deal.get("deposit_preference") or "").split())
        if pref == planner.SPLIT:
            note(deal, child_id, stage, deal.get("deposit_amount"), "split_check_by_hand")
            continue
        try:
            amount = planner.parse_amount(deal.get("deposit_amount"))
            _code, institution_id = derive.site_for_location(deal.get("location_of_interest"))
        except (planner.Hold, derive.Hold) as exc:
            note(deal, child_id, stage, deal.get("deposit_amount"), exc.reason)
            continue
        if institution_id not in payer_cache:
            try:
                payer_cache[institution_id] = _payers_by_child(public, institution_id)
            except payer_scan.ScanFailed:
                raise Stop("bill payer scan failed") from None
        payers = payer_cache[institution_id].get(child_id, [])
        if len(payers) != 1:
            note(deal, child_id, stage, amount, "bill_payer_missing" if not payers else "bill_payer_ambiguous")
            continue
        before = famly.list_deposits(rest, institution_id, child_id)
        if before:
            note(deal, child_id, stage, amount, "deposit_exists")
            continue
        if not confirm:
            note(deal, child_id, stage, amount, "would_add_paid")
            continue
        if limit is not None and written >= limit:
            note(deal, child_id, stage, amount, "limit_reached")
            continue
        written += 1
        writer.add_deposits(institution_id, child_id, payers[0],
                            [{"amount": amount, "date": today.isoformat(), "status": famly.STATUS_PAID,
                              "note": "Deposit (backfill)"}])
        after = famly.list_deposits(rest, institution_id, child_id)
        if (len(after) != 1 or famly.amount_read(after[0].get("amount")) != amount
                or after[0].get("status") != "PAYED_EXTERNALLY"):
            note(deal, child_id, stage, amount, "readback_mismatch")
            _write_csv(rows, out_path, echo)
            raise Stop(f"read-back mismatch on child {child_id}; run stopped")
        note(deal, child_id, stage, amount, "added_paid")

    _write_csv(rows, out_path, echo)
    echo(f"deposit-backfill ({'CONFIRM' if confirm else 'dry run'}): {len(deals)} deal(s); {counts}")
    return counts


def _write_csv(rows, out_path: Path | None, echo) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_path or Path("reports") / f"deposit_backfill_{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["deal_id", "child_id", "stage", "preference", "amount", "outcome"])
        w.writeheader()
        w.writerows(rows)
    echo(f"report: {path}")
