"""registry-backfill: fill the children registry from Famly and HubSpot (READ-ONLY on both). See backfill.py.
Dry run unless --confirm. Prints counts and codes only; --out writes ids and codes only (use reports/)."""

import argparse

NAME = "registry-backfill"
HELP = ("READ-ONLY on Famly and HubSpot: fill the children registry (var/, gitignored) with every Famly child, "
        "its URNs and its operations and ad hoc deals. Dry run unless --confirm")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm", action="store_true", help="Write to the registry (default: dry run)")
    parser.add_argument("--out", metavar="PATH", help="Also write a CSV of ids and codes (use reports/)")


def handle(args: argparse.Namespace):
    from actions.onboard_audit import runner as audit
    from actions.urn_lookup import runner as urn_runner

    from . import backfill

    hubspot = audit.ReadOnlyHubSpot()
    gql = audit.ReadOnlyGraphQL(urn_runner._public_client())
    summary, report = backfill.run(hubspot, gql, confirm=args.confirm)
    if args.out:
        summary["csv"] = {"rows": backfill.write_csv(args.out, report)}
    summary["requests"] = {"hubspot": hubspot.calls, "famly": gql.calls}
    return summary
