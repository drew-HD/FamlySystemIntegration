"""registry-refresh: the nightly refresh of the children registry (cron). See refresh.py.
READ-ONLY on Famly and HubSpot. Exit 1 when it stopped before writing or a due Slack post failed."""

import argparse
import logging
import sys

from core.logging_setup import configure_logging

NAME = "registry-refresh"
HELP = ("Nightly: refresh the children registry from Famly and HubSpot (READ-ONLY on both), flag children gone "
        "from Famly, post NEW issues to Slack (ids only). --dry-run: no write, no state, no Slack")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Read and report only: no registry write, no Slack")


def handle(args: argparse.Namespace):
    from actions.onboard_audit import runner as audit
    from actions.urn_lookup import runner as urn_runner

    from . import refresh

    configure_logging()
    hubspot = audit.ReadOnlyHubSpot()
    gql = audit.ReadOnlyGraphQL(urn_runner._public_client())
    try:
        result = refresh.run(hubspot, gql, dry_run=args.dry_run)
    except refresh.RefreshAborted as exc:
        print(f"registry refresh stopped: {exc.code} (nothing written)", file=sys.stderr)
        raise SystemExit(1) from None
    result["requests"] = {"hubspot": hubspot.calls, "famly": gql.calls}
    if result.get("postFailed"):
        logging.getLogger(__name__).error("registry-refresh: Slack post failed, state not saved")
        print("registry refresh: Slack post failed, state not saved", file=sys.stderr)
        raise SystemExit(1)
    return result
