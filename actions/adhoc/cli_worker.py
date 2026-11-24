"""adhoc-worker: process queued ad hoc jobs (dry mode: read-only). `--once` runs at most one job and exits; without
it the worker polls (what a systemd unit would run). Prints codes and ids only."""

import argparse

from core.logging_setup import configure_logging

from . import worker

NAME = "adhoc-worker"
HELP = "Run the ad hoc worker (step 1: dry mode, read-only; ADHOC_MODE must be dry)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--once", action="store_true", help="Recover, run at most one job, and exit")
    parser.add_argument("--poll", type=float, default=worker.POLL_SECONDS,
                        help=f"Seconds between polls when the queue is empty (default {worker.POLL_SECONDS})")


def handle(args: argparse.Namespace):
    configure_logging()
    if args.once:
        return {"recovered": worker.recover(), **worker.run_once()}
    worker.run_forever(args.poll)
    return {"stopped": True}
