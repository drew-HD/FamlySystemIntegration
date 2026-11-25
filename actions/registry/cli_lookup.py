"""registry-lookup: find a child in the children registry. Prints names and dates of birth to the terminal
only (CLAUDE.md allows names on the terminal); never emails. Nothing is logged."""

import argparse
import sys

from . import match, store

NAME = "registry-lookup"
HELP = ("Find a child in the children registry by Famly id, URN, deal id, parent email, or name + date of "
        "birth. Prints to the terminal only")


def add_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--child-id", metavar="ID", help="Famly child id")
    group.add_argument("--urn", metavar="URN", help="Child URN, e.g. HD0123CW")
    group.add_argument("--deal-id", metavar="ID", help="HubSpot deal id (operations or ad hoc)")
    group.add_argument("--email", metavar="EMAIL", help="A parent email (the email is not printed back)")
    group.add_argument("--name", metavar="NAME", help="Child name; needs --dob")
    parser.add_argument("--dob", metavar="YYYY-MM-DD", help="Date of birth, with --name")


def _show(child: store.Child, result: str | None = None) -> dict:
    out = {"famlyChildId": child.famly_child_id, "name": child.full_name, "dob": child.dob,
           "site": child.site_code, "urns": child.urns,
           "operationsDeals": [d for d, k in child.deals if k == store.OPERATIONS],
           "adhocDeals": [d for d, k in child.deals if k == store.ADHOC],
           "source": child.source, "updatedAt": child.updated_at}
    if result:
        out["match"] = result
    return out


def handle(args: argparse.Namespace):
    if args.name is not None:
        if not args.dob:
            print("--name needs --dob", file=sys.stderr)
            raise SystemExit(3)
        hits = match.find(args.name, args.dob, store.by_dob(args.dob))
        return {"found": len(hits), "children": [_show(c, r) for r, c in hits]}
    if args.dob:
        print("--dob only applies with --name", file=sys.stderr)
        raise SystemExit(3)
    if args.email:
        found = store.by_email(args.email)
    else:
        one = (store.by_child_id(args.child_id) if args.child_id else
               store.by_urn(args.urn) if args.urn else store.by_deal(args.deal_id))
        found = [one] if one else []
    return {"found": len(found), "children": [_show(c) for c in found]}
