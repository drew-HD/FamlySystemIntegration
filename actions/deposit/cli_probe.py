"""deposit-probe: check the deposit and invoice requests on one verified TEST child. See probe.py."""

import argparse
import sys

from core.client import GraphQLClient
from core.rest_client import RestClient

from . import probe

NAME = "deposit-probe"
HELP = "Deposit/invoice probe on ONE verified test child (read; add, add-paid, invoice need --confirm)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--step", action="append", choices=probe.STEPS, required=True)
    parser.add_argument("--confirm", action="store_true", help=f"Required for writes (at most {probe.MAX_WRITES})")
    parser.add_argument("--site", required=True, choices=("HDCW", "HDCITY", "HDWEST"))
    parser.add_argument("--child-id", required=True)
    parser.add_argument("--bill-payer-id", required=True)


def handle(args: argparse.Namespace):
    try:
        return probe.run(args.step, args.confirm, args.site, args.child_id, args.bill_payer_id,
                         RestClient(), GraphQLClient())
    except probe.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
