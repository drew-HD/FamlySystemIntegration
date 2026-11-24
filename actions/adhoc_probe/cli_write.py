"""adhoc-probe-write: WRITE stage of the ad hoc probe, TEST children only (needs --confirm and --step).
See writer.py for every guard."""

import argparse
import sys

from core.client import GraphQLClient
from core.rest_client import RestClient

from . import writer

NAME = "adhoc-probe-write"
HELP = ("Ad hoc probe WRITES on the AATest children only: remove end date, assign the Ad Hoc Only tag, one "
        "booking-then-delete cycle (needs --confirm and --step)")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm", action="store_true",
                        help=f"Required. At most {writer.MAX_WRITES} write requests in total")
    parser.add_argument("--step", action="append", choices=writer.STEPS, metavar="STEP",
                        help=f"One of {', '.join(writer.STEPS)} (repeatable; at least one)")
    parser.add_argument("--booking-site", default="HDCW", help="Which test child the booking cycle uses")


def handle(args: argparse.Namespace):
    try:
        return writer.run(args.confirm, args.step or [], args.booking_site, RestClient(), GraphQLClient())
    except writer.WriteRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
