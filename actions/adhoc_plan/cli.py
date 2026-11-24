"""adhoc-dry-run: READ-ONLY plan of what the ad hoc pipeline would do for the named deals. See planner.py.
Prints ids, codes, dates and prices only; --out (default reports/) saves the same as JSON."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import planner

NAME = "adhoc-dry-run"
HELP = ("READ-ONLY: show what the ad hoc pipeline would do for these deals (match, tag, end date, bookings, "
        "prices, holds). Never writes; no names or emails are printed")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", action="append", metavar="ID", help="A deal id (repeatable)")
    parser.add_argument("--file", metavar="PATH", help="A file of deal ids (digits only, one per line)")
    parser.add_argument("--out", metavar="PATH", help="Save the plans as JSON here (default: reports/)")


def handle(args: argparse.Namespace):
    from actions.onboard import batch

    from .readers import Readers

    ids = list(args.deal_id or [])
    if args.file:
        ids += batch.read_file(args.file)[0]
    if not ids or not all(i.isdigit() for i in ids):
        print("give --deal-id (digits only) or --file", file=sys.stderr)
        raise SystemExit(3)

    readers = Readers()
    index = readers.famly_index()                   # one Famly pull for every deal in this run
    results = []
    for deal_id in ids:
        plan = readers.plan_deal(deal_id, index)
        if "error" in plan:
            print(f"deal {deal_id}: {plan['error']}", file=sys.stderr)
            continue
        results.append(plan)
        print("\n".join(planner.describe(plan)))

    out = Path(args.out) if args.out else Path("reports") / f"adhoc_dry_run_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return {"plans": len(results), "withHolds": sum(1 for p in results if p["holds"]),
            "requests": {"hubspot": readers.hubspot.calls,
                         "famlyGraphql": readers.public.calls + readers.internal.calls,
                         "famlyRest": readers.rest.calls}, "saved": str(out)}
