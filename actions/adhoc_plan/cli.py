"""adhoc-dry-run: READ-ONLY plan of what the ad hoc pipeline would do for the named deals. See planner.py.
Prints ids, codes, dates and prices only; --out (default reports/) saves the same as JSON."""

import argparse
import json
import sys
from datetime import date, datetime, timezone
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
    from actions.adhoc_audit import runner as adhoc
    from actions.adhoc_probe import runner as reads
    from actions.onboard import batch
    from actions.onboard_audit import runner as audit
    from actions.onboard_dry_run import derive, links
    from actions.read_child_plans import runner as plans_runner
    from actions.urn_lookup import runner as urn_runner
    from core.client import GraphQLClient
    from core.rest_client import RestClient

    ids = list(args.deal_id or [])
    if args.file:
        ids += batch.read_file(args.file)[0]
    if not ids or not all(i.isdigit() for i in ids):
        print("give --deal-id (digits only) or --file", file=sys.stderr)
        raise SystemExit(3)

    hubspot = audit.ReadOnlyHubSpot()
    public = audit.ReadOnlyGraphQL(urn_runner._public_client())
    internal = audit.ReadOnlyGraphQL(GraphQLClient())
    rest = audit.ReadOnlyRest(RestClient())
    stages = hubspot.stage_names()
    index = audit.Index(audit.fetch_children(public, urn_runner._all_institution_ids()))
    try:
        test_children = reads.load_test_children()
    except reads.ProbeRefused:
        test_children = {}

    def read_sessions(child_id, day):
        return internal.execute(query_path=reads.SESSIONS_QUERY, operation_name=reads.SESSIONS_OP,
                                variables={"childIds": [child_id], "validOnDate": day.isoformat()})

    def preview(child_id, purchases, day):
        body = internal.execute(query_path=reads.CONFLICTS_QUERY, operation_name=reads.CONFLICTS_OP,
                                variables={"childId": child_id, "purchases": purchases, "date": day.isoformat()})
        return (((body.get("data") or {}).get("finance") or {}).get("purchases") or {}).get("previewUpdate")

    deps = planner.Deps(
        has_plan=lambda cid: bool(plans_runner.run(cid, client=rest).plans),
        read_sessions=read_sessions, preview=preview,
        price_child_for_site=lambda site: test_children.get(site))

    labels = links.load_labels(hubspot)
    results = []
    for deal in adhoc.deals_by_id(hubspot, ids):
        stage = stages.get(str(deal.get("dealstage")), ("", ""))[1] or None
        try:
            contacts = links.deal_contacts(hubspot, labels, str(deal["id"]))
        except Exception:  # noqa: BLE001
            contacts = None
        plan = planner.build_plan(deal, stage, contacts, index, deps, date.today())
        if contacts is None:
            plan["flags"].append("contacts_not_readable")
        results.append(plan)
        print("\n".join(planner.describe(plan)))
    missing = set(ids) - {p["dealId"] for p in results}
    if missing:
        print(f"not found: {', '.join(sorted(missing))}", file=sys.stderr)

    out = Path(args.out) if args.out else Path("reports") / f"adhoc_dry_run_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return {"plans": len(results), "withHolds": sum(1 for p in results if p["holds"]),
            "requests": {"hubspot": hubspot.calls, "famlyGraphql": public.calls + internal.calls,
                         "famlyRest": rest.calls}, "saved": str(out)}
