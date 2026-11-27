"""registry-backfill: fill the children registry from Famly and HubSpot (READ-ONLY on both). See backfill.py.
Dry run unless --confirm. Prints counts and codes only; --out writes ids and codes only (use reports/)."""

import argparse

NAME = "registry-backfill"
HELP = ("READ-ONLY on Famly and HubSpot: fill the children registry (var/, gitignored) with every Famly child, "
        "its URNs and its operations and ad hoc deals. Dry run unless --confirm")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm", action="store_true", help="Write to the registry (default: dry run)")
    parser.add_argument("--out", metavar="PATH", help="Also write a CSV of ids and codes (use reports/)")
    parser.add_argument("--deals-out", metavar="PATH",
                        help="Write the deal ids that have no Famly child, one per line (use reports/). "
                             "Left Nursery, Ad Hoc Completed and Closed Lost deals are left out. Never stored")


def handle(args: argparse.Namespace):
    from actions.onboard_audit import runner as audit
    from actions.urn_lookup import runner as urn_runner

    from . import backfill

    hubspot = audit.ReadOnlyHubSpot()
    gql = audit.ReadOnlyGraphQL(urn_runner._public_client())
    summary, report = backfill.run(hubspot, gql, confirm=args.confirm)
    no_child = summary.pop("_no_child_deals")
    summary.pop("_closed_deals")
    summary.pop("_child_deals")
    summary.pop("_deal_names")                  # names: never in the printed summary
    if args.deals_out:
        by_stage = {}
        for _, kind, stage in no_child:
            by_stage[f"{kind}:{stage or 'unknown'}"] = by_stage.get(f"{kind}:{stage or 'unknown'}", 0) + 1
        summary["deals_out"] = {"rows": backfill.write_deal_list(args.deals_out, no_child),
                                "by_stage": dict(sorted(by_stage.items()))}
    if args.out:
        summary["csv"] = {"rows": backfill.write_csv(args.out, report)}
    summary["requests"] = {"hubspot": hubspot.calls, "famly": gql.calls}
    return summary
