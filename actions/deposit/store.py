"""Durable record of deposit jobs: its OWN SQLite file (var/deposit_jobs.sqlite3, gitignored).

Rows hold ids, codes, amounts and dates only: never a name, email, phone, address or date of birth.

deposit_jobs
    id, deal_id, status, hold_code, resume_status, child_id, institution_id, bill_payer_id, preference,
    total, plan_start, approval_nonce, slack_ref, created_at, updated_at
    status: waiting | awaiting_approval | approved | scheduled | done | dry_done | held | rejected
      waiting            the child, bill payer or plans are not in Famly yet (re-checked each run)
      awaiting_approval  the Slack approval is posted
      approved           Approve was clicked; the worker writes installment 1 on its next run
      scheduled          installment 1 is done; later installments wait for their dates
      held               a person must look (hold_code says why); `deposit-resume` returns it to resume_status
    ONE ACTIVE JOB PER DEAL (unique partial index): active = waiting, awaiting_approval, approved, scheduled, held.

deposit_installments  (job_id, number) primary key
    amount (text, pounds), on_date, invoice_on (its own date; on that date a later installment is marked
    `covered` instead of invoiced when Famly's regular run bills the child that month)
    deposit_state: pending | sending | added       deposit_id
    invoice_state: none | due | sending | sent | covered      invoice_id
    `sending` is set BEFORE the request, in one atomic UPDATE, so a crash mid-request leaves a marker
    that is never resent automatically.
"""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "deposit_jobs.sqlite3"

WAITING, AWAITING_APPROVAL, APPROVED, SCHEDULED = "waiting", "awaiting_approval", "approved", "scheduled"
DONE, DRY_DONE, HELD, REJECTED = "done", "dry_done", "held", "rejected"
ACTIVE = (WAITING, AWAITING_APPROVAL, APPROVED, SCHEDULED, HELD)

D_PENDING, D_SENDING, D_ADDED = "pending", "sending", "added"
I_NONE, I_DUE, I_SENDING, I_SENT, I_COVERED = "none", "due", "sending", "sent", "covered"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS deposit_jobs (
    id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    hold_code TEXT,
    resume_status TEXT,
    child_id TEXT,
    institution_id TEXT,
    bill_payer_id TEXT,
    preference TEXT,
    total TEXT,
    plan_start TEXT,
    approval_nonce TEXT,
    slack_ref TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS deposit_one_active_per_deal ON deposit_jobs (deal_id)
    WHERE status IN ({", ".join(repr(s) for s in ACTIVE)});
CREATE TABLE IF NOT EXISTS deposit_installments (
    job_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    amount TEXT NOT NULL,
    on_date TEXT NOT NULL,
    invoice_on TEXT,
    deposit_state TEXT NOT NULL DEFAULT 'pending',
    deposit_id TEXT,
    invoice_state TEXT NOT NULL DEFAULT 'none',
    invoice_id TEXT,
    PRIMARY KEY (job_id, number)
);
"""

_JOB_FIELDS = {"status", "hold_code", "resume_status", "child_id", "institution_id", "bill_payer_id",
               "preference", "total", "plan_start", "approval_nonce", "slack_ref"}
_INST_FIELDS = {"deposit_state", "deposit_id", "invoice_state", "invoice_id", "invoice_on"}


@dataclass
class Job:
    id: str
    deal_id: str
    status: str
    hold_code: str | None
    resume_status: str | None
    child_id: str | None
    institution_id: str | None
    bill_payer_id: str | None
    preference: str | None
    total: str | None
    plan_start: str | None
    approval_nonce: str | None
    slack_ref: str | None
    source: str | None
    created_at: str
    updated_at: str


@dataclass
class Inst:
    job_id: str
    number: int
    amount: Decimal
    on_date: str
    invoice_on: str | None
    deposit_state: str
    deposit_id: str | None
    invoice_state: str
    invoice_id: str | None


def store_path() -> Path:
    raw = os.environ.get("DEPOSIT_STORE_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_STORE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _open():
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        yield connection
    finally:
        connection.close()


def _job(row) -> Job | None:
    return Job(**dict(row)) if row else None


def _inst(row) -> Inst:
    data = dict(row)
    data["amount"] = Decimal(data["amount"])
    return Inst(**data)


CREATED, ALREADY_ACTIVE, ALREADY_DONE = "created", "already_active", "already_done"


def enqueue(deal_id: str, source: str = "cli", *, reopen: bool = False) -> tuple[str, Job]:
    """One job per deal. A deal whose deposits are already done gets no new job unless `reopen`."""
    with _open() as c:
        active = c.execute(f"SELECT * FROM deposit_jobs WHERE deal_id=? AND status IN "
                           f"({','.join('?' * len(ACTIVE))})", (deal_id, *ACTIVE)).fetchone()
        if active:
            return ALREADY_ACTIVE, _job(active)
        done = c.execute("SELECT * FROM deposit_jobs WHERE deal_id=? AND status=? ORDER BY created_at DESC",
                         (deal_id, DONE)).fetchone()
        if done and not reopen:
            return ALREADY_DONE, _job(done)
        job_id, now = str(uuid.uuid4()), _now()
        try:
            c.execute("INSERT INTO deposit_jobs (id, deal_id, status, source, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (job_id, deal_id, WAITING, source, now, now))
        except sqlite3.IntegrityError:            # a concurrent enqueue won
            row = c.execute("SELECT * FROM deposit_jobs WHERE deal_id=? AND status IN "
                            f"({','.join('?' * len(ACTIVE))})", (deal_id, *ACTIVE)).fetchone()
            return ALREADY_ACTIVE, _job(row)
        return CREATED, _job(c.execute("SELECT * FROM deposit_jobs WHERE id=?", (job_id,)).fetchone())


def get(job_id: str) -> Job | None:
    with _open() as c:
        return _job(c.execute("SELECT * FROM deposit_jobs WHERE id=?", (job_id,)).fetchone())


def jobs(statuses=ACTIVE) -> list[Job]:
    with _open() as c:
        rows = c.execute(f"SELECT * FROM deposit_jobs WHERE status IN ({','.join('?' * len(statuses))}) "
                         "ORDER BY created_at", tuple(statuses)).fetchall()
    return [_job(r) for r in rows]


def update(job_id: str, **fields) -> None:
    bad = set(fields) - _JOB_FIELDS
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    with _open() as c:
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE deposit_jobs SET {sets}, updated_at=? WHERE id=?", (*fields.values(), _now(), job_id))


def transition(job_id: str, from_status: str, to_status: str, **fields) -> bool:
    """Atomic: only if the job is still in `from_status`."""
    bad = set(fields) - _JOB_FIELDS
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    with _open() as c:
        sets = "".join(f", {k}=?" for k in fields)
        cur = c.execute(f"UPDATE deposit_jobs SET status=?{sets}, updated_at=? WHERE id=? AND status=?",
                        (to_status, *fields.values(), _now(), job_id, from_status))
        return cur.rowcount == 1


def hold(job: Job, code: str) -> None:
    resume = job.status if job.status != HELD else job.resume_status
    update(job.id, status=HELD, hold_code=code, resume_status=resume)


def resume(job_id: str) -> bool:
    job = get(job_id)
    if job is None or job.status != HELD or not job.resume_status:
        return False
    return transition(job_id, HELD, job.resume_status, hold_code=None, resume_status=None)


def cancel(job_id: str) -> bool:
    """A held, waiting or awaiting job is closed by hand (nothing is undone in Famly)."""
    job = get(job_id)
    if job is None or job.status not in (HELD, WAITING, AWAITING_APPROVAL):
        return False
    return transition(job_id, job.status, REJECTED)


def save_installments(job_id: str, rows: list[dict]) -> None:
    """Replace the job's installments (only before anything was written)."""
    with _open() as c:
        c.execute("BEGIN IMMEDIATE")
        written = c.execute("SELECT COUNT(*) FROM deposit_installments WHERE job_id=? AND "
                            "(deposit_state != 'pending' OR invoice_state NOT IN ('none', 'due', 'covered'))",
                            (job_id,)).fetchone()[0]
        if written:
            c.execute("ROLLBACK")
            raise RuntimeError("installments already written; refusing to replace them")
        c.execute("DELETE FROM deposit_installments WHERE job_id=?", (job_id,))
        for r in rows:
            c.execute("INSERT INTO deposit_installments (job_id, number, amount, on_date, invoice_on, invoice_state)"
                      " VALUES (?, ?, ?, ?, ?, ?)",
                      (job_id, r["number"], str(r["amount"]), r["on_date"], r["invoice_on"],
                       I_DUE if r["invoice_on"] else I_COVERED))
        c.execute("COMMIT")


def installments(job_id: str) -> list[Inst]:
    with _open() as c:
        rows = c.execute("SELECT * FROM deposit_installments WHERE job_id=? ORDER BY number", (job_id,)).fetchall()
    return [_inst(r) for r in rows]


def inst_transition(job_id: str, number: int, column: str, from_state: str, to_state: str, **fields) -> bool:
    if column not in ("deposit_state", "invoice_state") or set(fields) - _INST_FIELDS:
        raise ValueError("bad installment update")
    with _open() as c:
        sets = "".join(f", {k}=?" for k in fields)
        cur = c.execute(f"UPDATE deposit_installments SET {column}=?{sets} WHERE job_id=? AND number=? "
                        f"AND {column}=?", (to_state, *fields.values(), job_id, number, from_state))
        return cur.rowcount == 1


def all_deposit_ids(child_id: str) -> set[str]:
    """Every Famly deposit id this pipeline added for the child (across jobs)."""
    with _open() as c:
        rows = c.execute("SELECT i.deposit_id FROM deposit_installments i JOIN deposit_jobs j ON j.id=i.job_id "
                         "WHERE j.child_id=? AND i.deposit_id IS NOT NULL", (child_id,)).fetchall()
    return {r[0] for r in rows}


def status_counts() -> dict[str, int]:
    with _open() as c:
        return {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM deposit_jobs GROUP BY status")}


def _reset_for_tests() -> None:
    with _open() as c:
        c.execute("DELETE FROM deposit_jobs")
        c.execute("DELETE FROM deposit_installments")
