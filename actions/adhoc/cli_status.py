"""adhoc-status: counts of ad hoc jobs by status, or the latest job for a deal. Local file only; codes and ids."""

import argparse

from . import config, store

NAME = "adhoc-status"
HELP = "Show ad hoc job counts by status, or the latest job for --deal-id"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", metavar="ID", help="Show the latest job for this deal")


def handle(args: argparse.Namespace):
    if not args.deal_id:
        return {"mode": config.mode(), "counts": store.status_counts()}
    job = store.latest_for_deal(args.deal_id)
    if job is None:
        return {"dealId": args.deal_id, "job": None}
    plan = job.plan or {}
    return {"dealId": job.deal_id, "jobId": job.id, "status": job.status, "mode": job.mode, "source": job.source,
            "holds": job.holds, "flags": job.flags, "childId": job.child_id, "attempts": job.attempts,
            "bookings": len(plan.get("bookings") or []), "total": plan.get("total"),
            "discountAmount": plan.get("discountAmount"), "steps": plan.get("steps"), "updatedAt": job.updated_at}
