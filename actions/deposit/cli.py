"""Deposit pipeline commands: deposit-worker, deposit-enqueue, deposit-status."""

import argparse

from actions.pull_roster.runner import _public_client
from core.client import GraphQLClient
from core.hubspot import HubSpotClient
from core.rest_client import RestClient

from . import config, store


class Worker:
    NAME = "deposit-worker"
    HELP = "One pass of the deposit pipeline (cron, every 10 minutes). Needs DEPOSIT_MODE dry or live"

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        pass

    @staticmethod
    def handle(args: argparse.Namespace):
        from .notifier import SlackNotifier
        from .worker import Worker as W
        if config.mode() == config.MODE_OFF:
            print("DEPOSIT_MODE is off; nothing done")
            return 0
        counts = W(HubSpotClient(), RestClient(), GraphQLClient(), _public_client(), SlackNotifier()).run_once()
        print(f"deposit-worker: {counts['jobs']} job(s) looked at; {store.status_counts()}")
        return 0


class Enqueue:
    NAME = "deposit-enqueue"
    HELP = "Queue a deal for the deposit pipeline (same as the webhook)"

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--deal-id", required=True)
        parser.add_argument("--reopen-done", action="store_true", help="Queue again even if the deal is done")

    @staticmethod
    def handle(args: argparse.Namespace):
        if not str(args.deal_id).isdigit():
            print("--deal-id must be digits only")
            return 2
        result, job = store.enqueue(str(args.deal_id), source="cli", reopen=args.reopen_done)
        print(f"{result}: job {job.id} ({job.status})")
        return 0


class Backfill:
    NAME = "deposit-backfill"
    HELP = ("Record existing Operations deals' deposits in Famly (paid stages: one PAID deposit; Deposit Pending: "
            "queued for the pipeline). Dry run unless --confirm")

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--confirm", action="store_true", help="Write (needs DEPOSIT_MODE=live)")
        parser.add_argument("--limit", type=int, default=None, help="At most N deposits written this run")
        parser.add_argument("--out", default=None, help="CSV path (default reports/deposit_backfill_<time>.csv)")

    @staticmethod
    def handle(args: argparse.Namespace):
        from pathlib import Path

        from . import backfill
        try:
            backfill.run(HubSpotClient(), RestClient(), _public_client(), confirm=args.confirm, limit=args.limit,
                         out_path=Path(args.out) if args.out else None)
        except backfill.Stop as exc:
            print(f"STOPPED: {exc}")
            return 3
        return 0


class Status:
    NAME = "deposit-status"
    HELP = "List deposit jobs and their installments; --resume JOB or --cancel JOB a held job"

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--resume", metavar="JOB_ID")
        parser.add_argument("--cancel", metavar="JOB_ID")
        parser.add_argument("--all", action="store_true", help="Include finished jobs")

    @staticmethod
    def handle(args: argparse.Namespace):
        if args.resume:
            print("resumed" if store.resume(args.resume) else "not resumed (not held, or unknown job)")
            return 0
        if args.cancel:
            print("cancelled" if store.cancel(args.cancel) else "not cancelled (job is not held/waiting/awaiting)")
            return 0
        statuses = store.ACTIVE + ((store.DONE, store.DRY_DONE, store.REJECTED) if args.all else ())
        for job in store.jobs(statuses):
            print(f"{job.id}  deal {job.deal_id}  {job.status}{'  ' + job.hold_code if job.hold_code else ''}"
                  f"  {job.preference or ''}  {job.total or ''}")
            for i in store.installments(job.id):
                print(f"    {i.number}: {i.amount} on {i.on_date}  deposit {i.deposit_state}"
                      f"  invoice {i.invoice_state}{' ' + i.invoice_on if i.invoice_on else ''}")
        print(store.status_counts())
        return 0
