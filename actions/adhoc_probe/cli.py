"""adhoc-probe-read: READ-ONLY probe of Famly's internal ad hoc endpoints on the test children. See runner.py."""

import argparse
import sys

from core.client import GraphQLClient
from core.rest_client import RestClient

from . import runner

NAME = "adhoc-probe-read"
HELP = ("READ-ONLY probe on the test children: sessions and prices, booking-conflict preview, tag list. "
        "Never writes")


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    try:
        return runner.run_reads(RestClient(), GraphQLClient())
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
