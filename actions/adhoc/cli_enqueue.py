"""adhoc-enqueue: queue ad hoc deals for the worker. Writes only to the local jobs file; no Famly or HubSpot call."""

import argparse
import sys

from . import config, store

NAME = "adhoc-enqueue"
HELP = "Queue Ad Hoc deals for the ad hoc worker (refused while ADHOC_MODE is off)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", action="append", metavar="ID", help="A deal id, digits only (repeatable)")
    parser.add_argument("--file", metavar="PATH", help="A file of deal ids (digits only, one per line)")


def handle(args: argparse.Namespace):
    run_mode = config.mode()
    if run_mode == config.MODE_OFF:
        print("ADHOC_MODE is off; nothing was queued", file=sys.stderr)
        raise SystemExit(3)
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
