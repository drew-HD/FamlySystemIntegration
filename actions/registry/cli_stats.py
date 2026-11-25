"""registry-stats: counts from the children registry (no names)."""

import argparse

from . import store

NAME = "registry-stats"
HELP = "Counts from the children registry: children, URNs, deals, gaps. No names"


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return {"store": store.store_path().name, **store.stats()}
