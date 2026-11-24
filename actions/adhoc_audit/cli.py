"""adhoc-audit: READ-ONLY audit of the Ad Hoc pipeline. See runner.py. Prints counts and codes only;
--out writes a CSV of ids and codes only (put it in reports/)."""

import argparse
import sys

from . import runner

NAME = "adhoc-audit"
HELP = ("READ-ONLY: audit the HubSpot Ad Hoc pipeline (booked dates and slots, child already in Famly, "
        "enrolment) as codes. Never writes; no names or values are printed")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", metavar="PATH",
                        help="Only these deals (digits only, one per line; blank lines and # comments ignored). "
                             "Default: every deal in the Ad Hoc pipeline")
    parser.add_argument("--stage", action="append", metavar="NAME",
                        help="Only deals in this stage label (repeatable); not with --file")
    parser.add_argument("--out", metavar="PATH", help="Also write a CSV of ids and codes (use reports/)")
    parser.add_argument("--codes", metavar="CODE1,CODE2", help="--out: only rows that have ANY of these codes")


def handle(args: argparse.Namespace):
    from actions.onboard import batch
    from actions.onboard_audit import runner as audit
    from actions.urn_lookup import runner as urn_runner
    from core.rest_client import RestClient

    if args.codes and not args.out:
        print("--codes restricts the CSV: give --out too", file=sys.stderr)
        raise SystemExit(3)
    if args.file and args.stage:
        print("--stage does not apply with --file", file=sys.stderr)
        raise SystemExit(3)
    deal_ids = None
    if args.file:
        deal_ids, _hash = batch.read_file(args.file)
        print(f"{len(deal_ids)} deal id(s) read", file=sys.stderr)

    hubspot = audit.ReadOnlyHubSpot()
    gql = audit.ReadOnlyGraphQL(urn_runner._public_client())
    rest = audit.ReadOnlyRest(RestClient())
    summary, rows = runner.run(hubspot, gql, rest, deal_ids=deal_ids, stage_names=args.stage)
    if args.out:
        written = runner.write_csv(args.out, rows, only_codes=(args.codes or "").split(","))
        summary["csv"] = {"rows": written}
    return summary
