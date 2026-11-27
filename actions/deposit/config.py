"""Deposit pipeline settings, read fresh from the environment on every call.

    DEPOSIT_MODE              off | dry | live (default off; anything else is off)
        off   the webhook refuses and the worker does nothing.
        dry   the worker reads and plans, posts the Slack approval, and on Approve writes NOTHING.
        live  Approve adds the first deposit and sends its invoice; later installments run on schedule.
              Every deal may write (no deal allow-list: Drew, 2026-11-27).
    DEPOSIT_INVOICE_DUE_DAYS  days from the invoice date to its due date (default 7).
    DEPOSIT_WAIT_DAYS         how long a job waits for the child, bill payer and plans before it is held
                              (default 14).
    DEPOSIT_STORE_PATH        the jobs SQLite file (default var/deposit_jobs.sqlite3, gitignored).
"""

import logging
import os

logger = logging.getLogger(__name__)

MODE_OFF, MODE_DRY, MODE_LIVE = "off", "dry", "live"
INVOICE_SEND_HOUR_LONDON = 8   # invoices are never sent before 08:00 London


def mode() -> str:
    raw = os.environ.get("DEPOSIT_MODE", "").strip().lower()
    if raw == "":
        return MODE_OFF
    if raw in (MODE_OFF, MODE_DRY, MODE_LIVE):
        return raw
    logger.error("DEPOSIT_MODE=%r is not off, dry or live; treating it as off", raw)
    return MODE_OFF


def writes_allowed(deal_id: str) -> bool:
    return mode() == MODE_LIVE


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.error("%s is not a whole number; using %s", name, default)
        return default
    return min(max(value, low), high)


def invoice_due_days() -> int:
    return _int("DEPOSIT_INVOICE_DUE_DAYS", 7, 0, 60)


def wait_days() -> int:
    return _int("DEPOSIT_WAIT_DAYS", 14, 1, 90)
