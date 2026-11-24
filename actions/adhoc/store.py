"""Durable record of ad hoc jobs: the `adhoc_jobs` table (its OWN SQLite file, separate from onboarding).

Same conventions as actions/onboard/store.py: stdlib sqlite3, WAL, a busy timeout so several processes share it,
created on first use, ONE atomic INSERT that a UNIQUE PARTIAL INDEX arbitrates, `created_at` stamped by SQLite.

A row holds ids, codes, hashes and the PLAN JSON, which carries Famly child ids, session dates, session titles
and prices only: never a name, email, phone, address or date of birth.

    id               uuid4
    deal_id          the HubSpot deal
    status           queued | held | awaiting_approval | writing | done | dry_done | needs_review | failed
    mode             off | dry | live, as the job was last processed
    source           cli | webhook   (a webhook job must be an Approved deal, else it is held `stage_not_eligible`)
    plan_hash        hash of the plan JSON last made
    plan_json        the plan (ids, codes, dates, prices)
    holds, flags     JSON lists of codes
    child_id         the Famly child, once matched (None for a new child)
    attempts, last_error_code, claimed_at, created_at, updated_at

ONE ACTIVE JOB PER DEAL, enforced by a unique partial index: active = queued, awaiting_approval, writing.
`held`, `needs_review`, `failed`, `dry_done` and `done` are finished (a person acts, then re-enqueues).
Re-enqueueing a deal that is `done` creates no job. A deal can have several jobs over time, and one CHILD can
have several ad hoc DEALS: every request is its own deal and its own job.

No Flask, no Famly: importable and testable anywhere.
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "adhoc_jobs.sqlite3"
STALE_CLAIM_SECONDS = 600

QUEUED, HELD, AWAITING_APPROVAL, WRITING = "queued", "held", "awaiting_approval", "writing"
DONE, DRY_DONE, NEEDS_REVIEW, FAILED = "done", "dry_done", "needs_review", "failed"
ALL_STATUSES = (QUEUED, HELD, AWAITING_APPROVAL, WRITING, DONE, DRY_DONE, NEEDS_REVIEW, FAILED)
ACTIVE = (QUEUED, AWAITING_APPROVAL, WRITING)
SOURCES = ("cli", "webhook")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adhoc_jobs (
    id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'cli',
    plan_hash TEXT,
    plan_json TEXT,
    holds TEXT,
    flags TEXT,
    child_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approval_nonce TEXT,
    approved_hash TEXT,
    approval_ref TEXT,
    reask_count INTEGER NOT NULL DEFAULT 0,
    question TEXT,
    candidate_child_id TEXT,
    match_decision TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS adhoc_one_active_per_deal
    ON adhoc_jobs (deal_id) WHERE status IN ('queued', 'awaiting_approval', 'writing');
CREATE INDEX IF NOT EXISTS adhoc_status ON adhoc_jobs (status, created_at);
"""

# Columns added after step 1 shipped (a store created then does not have them).
_ADDED_COLUMNS = (
    ("approval_nonce", "TEXT"), ("approved_hash", "TEXT"), ("approval_ref", "TEXT"),
    ("reask_count", "INTEGER NOT NULL DEFAULT 0"), ("question", "TEXT"), ("candidate_child_id", "TEXT"),
    ("match_decision", "TEXT"),
)

_UPDATABLE = {"mode", "plan_hash", "plan_json", "holds", "flags", "child_id", "last_error_code", "approval_nonce",
              "approved_hash", "approval_ref", "reask_count", "question", "candidate_child_id", "match_decision"}
_JSON_FIELDS = {"holds", "flags"}


@dataclass
class Job:
    id: str
    deal_id: str
    status: str
    mode: str
    source: str
    plan_hash: str | None
    child_id: str | None
    attempts: int
    last_error_code: str | None
    claimed_at: str | None
    created_at: str
    updated_at: str
    holds: list | None = None
    flags: list | None = None
    plan: dict | None = None
    approval_nonce: str | None = None
    approved_hash: str | None = None
    approval_ref: str | None = None
    reask_count: int = 0
    question: str | None = None            # what an awaiting_approval job is asking: "plan" or "match"
    candidate_child_id: str | None = None  # the Famly child a "match" question offers
    match_decision: str | None = None      # "confirmed" (the candidate is the child) | "new" (create a new child)


def store_path() -> Path:
    override = os.environ.get("ADHOC_STORE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _open():
    """Open the store, commit on success, and ALWAYS close."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        _migrate(connection)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns an older store lacks. An ADD COLUMN that loses a race to another process is the column
    already being there, so it is ignored; any other error is raised."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(adhoc_jobs)")}
    for name, ddl in _ADDED_COLUMNS:
        if name in columns:
            continue
        try:
            connection.execute(f"ALTER TABLE adhoc_jobs ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _loads(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"], deal_id=row["deal_id"], status=row["status"], mode=row["mode"], source=row["source"],
        plan_hash=row["plan_hash"], child_id=row["child_id"], attempts=row["attempts"],
        last_error_code=row["last_error_code"], claimed_at=row["claimed_at"], created_at=row["created_at"],
        updated_at=row["updated_at"], holds=_loads(row["holds"]), flags=_loads(row["flags"]),
        plan=_loads(row["plan_json"]), approval_nonce=row["approval_nonce"], approved_hash=row["approved_hash"],
        approval_ref=row["approval_ref"], reask_count=row["reask_count"], question=row["question"],
        candidate_child_id=row["candidate_child_id"], match_decision=row["match_decision"])


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #
CREATED, ALREADY_QUEUED, ALREADY_DONE = "created", "already_queued", "already_done"


@dataclass
class Enqueued:
    outcome: str
    job: Job


def enqueue(deal_id: str, mode: str, source: str = "cli") -> Enqueued:
    """Enqueue a job for `deal_id` with ONE atomic INSERT: a deal that is already `done` creates no job; a deal
    with an active job returns it (the unique index decides, not a check made first)."""
    if source not in SOURCES:
        raise ValueError(f"unknown job source: {source!r}")
    deal_id = str(deal_id).strip()
    with _open() as connection:
        done = connection.execute(
            "SELECT * FROM adhoc_jobs WHERE deal_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (deal_id, DONE)).fetchone()
        if done is not None:
            return Enqueued(ALREADY_DONE, _to_job(done))
    job_id = str(uuid.uuid4())
    try:
        with _open() as connection:
            connection.execute(
                "INSERT INTO adhoc_jobs (id, deal_id, status, mode, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                (job_id, deal_id, QUEUED, mode, source, _now()))
    except sqlite3.IntegrityError:
        active = active_for_deal(deal_id)
        if active is not None:
            return Enqueued(ALREADY_QUEUED, active)
        raise
    return Enqueued(CREATED, get(job_id))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get(job_id: str) -> Job | None:
    with _open() as connection:
        row = connection.execute("SELECT * FROM adhoc_jobs WHERE id = ?", (job_id,)).fetchone()
    return _to_job(row) if row else None


def active_for_deal(deal_id: str) -> Job | None:
    with _open() as connection:
        row = connection.execute(
            f"SELECT * FROM adhoc_jobs WHERE deal_id = ? AND status IN ({','.join('?' * len(ACTIVE))}) LIMIT 1",
            (str(deal_id), *ACTIVE)).fetchone()
    return _to_job(row) if row else None


def latest_for_deal(deal_id: str) -> Job | None:
    with _open() as connection:
        row = connection.execute(
            "SELECT * FROM adhoc_jobs WHERE deal_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (str(deal_id),)).fetchone()
    return _to_job(row) if row else None


def status_counts() -> dict[str, int]:
    with _open() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS n FROM adhoc_jobs GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


# --------------------------------------------------------------------------- #
# Worker transitions
# --------------------------------------------------------------------------- #
def claim_next() -> Job | None:
    """Take the OLDEST queued job, atomically. A claim older than STALE_CLAIM_SECONDS is abandoned and may be
    taken again."""
    now = _now()
    stale_before = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - STALE_CLAIM_SECONDS, timezone.utc).isoformat()
    with _open() as connection:
        row = connection.execute(
            "SELECT id FROM adhoc_jobs WHERE status = ? AND (claimed_at IS NULL OR claimed_at < ?) "
            "ORDER BY created_at, rowid LIMIT 1", (QUEUED, stale_before)).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            "UPDATE adhoc_jobs SET claimed_at = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = ? AND (claimed_at IS NULL OR claimed_at < ?)",
            (now, now, row["id"], QUEUED, stale_before))
        if cursor.rowcount != 1:
            return None
    return get(row["id"])


def set_status(job_id: str, status: str, **fields) -> None:
    """Set the status (and any updatable columns) and release the claim."""
    assert status in ALL_STATUSES, status
    _check_fields(fields)
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        connection.execute(
            f"UPDATE adhoc_jobs SET status = ?, claimed_at = NULL, updated_at = ?"
            f"{', ' + columns if columns else ''} WHERE id = ?", (status, _now(), *values, job_id))


def update(job_id: str, **fields) -> None:
    """Update columns without changing the status or the claim."""
    _check_fields(fields)
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        connection.execute(f"UPDATE adhoc_jobs SET {columns}, updated_at = ? WHERE id = ?",
                           (*values, _now(), job_id))


def transition(job_id: str, from_status: str, to_status: str, **fields) -> bool:
    """Atomic compare-and-set: change the status only if it is still `from_status`. Returns whether THIS call did
    it, so a double click, or a click on a job that already moved on, does nothing the second time."""
    assert to_status in ALL_STATUSES, to_status
    _check_fields(fields)
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        cursor = connection.execute(
            f"UPDATE adhoc_jobs SET status = ?, claimed_at = NULL, updated_at = ?"
            f"{', ' + columns if columns else ''} WHERE id = ? AND status = ?",
            (to_status, _now(), *values, job_id, from_status))
    return cursor.rowcount == 1


def consume_hold_nonce(job_id: str, nonce: str) -> bool:
    """Spend a held job's Retry nonce, atomically: only ONE press can win."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE adhoc_jobs SET approval_nonce = NULL, updated_at = ? "
            "WHERE id = ? AND status = ? AND approval_nonce = ?", (_now(), job_id, HELD, nonce))
    return cursor.rowcount == 1


def recover_in_flight() -> int:
    """A job found mid-write (`writing`) means a worker died after it may have written. It is NEVER replayed: it
    becomes `needs_review` (`interrupted`). Returns how many were moved."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE adhoc_jobs SET status = ?, last_error_code = ?, claimed_at = NULL, updated_at = ? "
            "WHERE status = ?", (NEEDS_REVIEW, "interrupted", _now(), WRITING))
    return cursor.rowcount


def _check_fields(fields: dict) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"not an updatable column: {sorted(bad)}")


def _encode(name: str, value):
    if name in _JSON_FIELDS and value is not None:
        return json.dumps(value)
    if name == "plan_json" and value is not None and not isinstance(value, str):
        return json.dumps(value, sort_keys=True)
    return value


def _reset_for_tests() -> None:
    with _open() as connection:
        connection.execute("DELETE FROM adhoc_jobs")
