"""create-test-children: create one persistent TEST child per site (needs --confirm). See runner.py."""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.rest_client import RestClient

from . import runner

NAME = "create-test-children"
HELP = ("Create one AATest TEST child at each of HDCITY, HDCW, HDWEST (isTestChild true; idempotent; "
        "creates only; needs --confirm)")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm", action="store_true",
                        help=f"Required. Allows up to {runner.MAX_CREATES} creates (never retried)")


def handle(args: argparse.Namespace):
    try:
        result = runner.run(args.confirm, RestClient(), _public_client())
    except runner.SeedRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {"created": result.created, "alreadyExisted": result.existing,
            "skipped": result.skipped, "createRequests": result.writes}
