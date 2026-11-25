"""adhoc-enqueue: queue ad hoc deals for the worker. Writes only to the local jobs file; no Famly or HubSpot call."""

import argparse
import sys
from datetime import date

from . import config, store

NAME = "adhoc-enqueue"
HELP = "Queue Ad Hoc deals for the ad hoc worker (refused while ADHOC_MODE is off)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", action="append", metavar="ID", help="A deal id, digits only (repeatable)")
    parser.add_argument("--file", metavar="PATH", help="A file of deal ids (digits only, one per line)")
    parser.add_argument("--future", action="store_true",
                        help="Backfill: queue Ad Hoc deals whose dates are all today or later (read-only HubSpot; "
                             "skips Closed Lost, Ad Hoc Completed and deals with an active or done job)")
    parser.add_argument("--stage", action="append", metavar="NAME", help="--future: only these stage labels")
    parser.add_argument("--limit", type=int, metavar="N", help="--future: queue at most N deals")
    parser.add_argument("--list-only", action="store_true", help="--future: print the deals, queue nothing")


def future_deals(hubspot, today=None, stage_names=None) -> list[tuple[str, str]]:
    """(deal id, stage label) for Ad Hoc deals with at least one date and every date >= today."""
    from actions.adhoc_audit import runner as audit_runner
    from actions.onboard_dry_run import derive
    today = today or date.today()
    stages = hubspot.stage_names()
    out = []
    for deal in audit_runner.adhoc_deals(hubspot, stages, stage_names):
        label = stages.get(str(deal.get("dealstage")), ("", ""))[1]
        if derive.norm_stage(label) in audit_runner.PAST_STAGES:
            continue
        dates = [audit_runner.parse_date(deal.get(p)) for p in audit_runner.DATE_PROPERTIES]
        dates = [d for d in dates if d]
        if dates and min(dates) >= today:
            out.append((str(deal.get("id") or deal.get("hs_object_id")), label))
    return sorted(out)


def backfill(hubspot, run_mode, *, today=None, stage_names=None, limit=None, list_only=False) -> dict:
    picked = []
    for deal_id, label in future_deals(hubspot, today, stage_names):
        latest = store.latest_for_deal(deal_id)
        if store.active_for_deal(deal_id) or (latest is not None and latest.status == store.DONE):
            continue
        picked.append((deal_id, label))
    if limit is not None:
        picked = picked[:limit]
    by_stage: dict[str, int] = {}
    for _id, label in picked:
        by_stage[label] = by_stage.get(label, 0) + 1
    result = {"mode": run_mode, "listOnly": list_only, "count": len(picked), "byStage": by_stage,
              "dealIds": [i for i, _ in picked]}
    if not list_only:
        result["jobs"] = [{"dealId": i, "outcome": (r := store.enqueue(i, run_mode, "cli")).outcome,
                           "jobId": r.job.id} for i, _ in picked]
    return result


def handle(args: argparse.Namespace):
    run_mode = config.mode()
    future = getattr(args, "future", False)
    if run_mode == config.MODE_OFF and not (future and getattr(args, "list_only", False)):
        print("ADHOC_MODE is off; nothing was queued", file=sys.stderr)
        raise SystemExit(3)
    if future:
        if args.deal_id or args.file:
            print("--future does not combine with --deal-id or --file", file=sys.stderr)
            raise SystemExit(3)
        from actions.onboard_audit import runner as onboard_audit
        return backfill(onboard_audit.ReadOnlyHubSpot(), run_mode, stage_names=args.stage, limit=args.limit,
                        list_only=args.list_only)
    ids = list(args.deal_id or [])
    if args.file:
        from actions.onboard import batch
        ids += batch.read_file(args.file)[0]
    if not ids or not all(i.isdigit() for i in ids):
        print("give --deal-id or --file (digits only)", file=sys.stderr)
        raise SystemExit(3)
    return {"mode": run_mode, "jobs": [
        {"dealId": i, "outcome": (r := store.enqueue(i, run_mode, "cli")).outcome, "jobId": r.job.id}
        for i in ids]}
