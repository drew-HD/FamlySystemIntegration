"""The children registry: one row per Famly child, tying together its name, date of birth, URNs, parent
emails, and the HubSpot deals (operations and ad hoc) that belong to it.

Its OWN SQLite file (CHILDREN_REGISTRY_PATH, default var/children_registry.sqlite3, gitignored). It holds
names, dates of birth and emails, which CLAUDE.md allows ONLY in gitignored folders: never commit it, never
log a name, email or date of birth from it. Same conventions as actions/adhoc/store.py (stdlib sqlite3,
WAL, busy timeout, created on first use).

A URN or deal id belongs to ONE child. Tying it to a second child is refused and returned as a conflict;
the first link is never overwritten (a person sorts it out). Famly is the source of truth for the name and
date of birth, so a later upsert replaces them.
"""

import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "children_registry.sqlite3"

OPERATIONS, ADHOC, WEEKEND_SCHOOL = "operations", "adhoc", "weekend_school"
DEAL_KINDS = (OPERATIONS, ADHOC, WEEKEND_SCHOOL)

# Results of add_urn / add_deal / add_email.
ADDED, ALREADY, CONFLICT = "added", "already", "conflict"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS children (
    famly_child_id TEXT PRIMARY KEY,
    full_name      TEXT,
    name_key       TEXT,
    dob            TEXT,
    site_code      TEXT,
    source         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS children_by_dob ON children (dob, name_key);
CREATE TABLE IF NOT EXISTS child_urns (
    urn            TEXT PRIMARY KEY,
    famly_child_id TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS child_urns_by_child ON child_urns (famly_child_id);
CREATE TABLE IF NOT EXISTS child_deals (
    deal_id        TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    famly_child_id TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS child_deals_by_child ON child_deals (famly_child_id);
CREATE TABLE IF NOT EXISTS child_emails (
    email          TEXT NOT NULL,
    famly_child_id TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (email, famly_child_id)
);
-- A person's answer to the same-child question for an operations deal (stage 4): same | different.
CREATE TABLE IF NOT EXISTS same_child_decisions (
    deal_id        TEXT NOT NULL,
    famly_child_id TEXT NOT NULL,
    decision       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (deal_id, famly_child_id)
);
-- Two children a person confirmed as siblings (Gate A "Same bill payer and siblings"); the pair is stored in id order.
CREATE TABLE IF NOT EXISTS sibling_links (
    child_a        TEXT NOT NULL,
    child_b        TEXT NOT NULL,
    deal_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (child_a, child_b)
);
"""

SAME, DIFFERENT = "same", "different"


@dataclass
class Child:
    famly_child_id: str
    full_name: str = field(default="", repr=False)
    dob: str = field(default="", repr=False)
    site_code: str | None = None
    source: str = ""
    urns: list = field(default_factory=list)
    deals: list = field(default_factory=list)          # (deal_id, kind)
    created_at: str = ""
    updated_at: str = ""


def store_path() -> Path:
    override = os.environ.get("CHILDREN_REGISTRY_PATH", "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def name_key(name: str) -> str:
    """Accents removed, casefolded, hyphens as spaces, tokens sorted: 'Zoë Smith-Jones' == 'jones smith zoe'."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().replace("-", " ")
    return " ".join(sorted(text.split()))


def _urn_key(urn: str) -> str:
    return str(urn or "").strip().casefold()


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
        yield connection
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def upsert_child(famly_child_id: str, full_name: str, dob: str, site_code: str | None, source: str) -> bool:
    """Add the child, or refresh its name, date of birth and site from Famly. True if it was new.
    `source` is kept from the first write."""
    child_id = str(famly_child_id).strip()
    if not child_id:
        raise ValueError("famly_child_id is required")
    now = _now()
    with _open() as connection:
        found = connection.execute("SELECT 1 FROM children WHERE famly_child_id = ?", (child_id,)).fetchone()
        if found:
            connection.execute(
                "UPDATE children SET full_name = ?, name_key = ?, dob = ?, site_code = COALESCE(?, site_code), "
                "updated_at = ? WHERE famly_child_id = ?",
                (full_name or "", name_key(full_name), dob or "", site_code, now, child_id))
            return False
        connection.execute(
            "INSERT INTO children (famly_child_id, full_name, name_key, dob, site_code, source, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (child_id, full_name or "", name_key(full_name), dob or "", site_code, source, now, now))
        return True


def _link(table: str, key_column: str, key: str, child_id: str, extra: dict | None = None) -> str:
    extra = extra or {}
    with _open() as connection:
        row = connection.execute(f"SELECT famly_child_id FROM {table} WHERE {key_column} = ?", (key,)).fetchone()
        if row:
            return ALREADY if row["famly_child_id"] == child_id else CONFLICT
        columns = [key_column, "famly_child_id", "created_at", *extra]
        values = [key, child_id, _now(), *extra.values()]
        try:
            connection.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                               f"({', '.join('?' for _ in columns)})", values)
        except sqlite3.IntegrityError:
            # Lost a race with another process: read again and answer from what is there.
            row = connection.execute(f"SELECT famly_child_id FROM {table} WHERE {key_column} = ?",
                                     (key,)).fetchone()
            return ALREADY if row and row["famly_child_id"] == child_id else CONFLICT
        return ADDED


def add_urn(famly_child_id: str, urn: str) -> str:
    """Tie a URN to the child: added | already | conflict (the URN belongs to another child; not changed)."""
    key = _urn_key(urn)
    if not key:
        raise ValueError("urn is required")
    return _link("child_urns", "urn", key, str(famly_child_id))


def add_deal(famly_child_id: str, deal_id: str, kind: str) -> str:
    """Tie a HubSpot deal to the child: added | already | conflict (the deal belongs to another child)."""
    if kind not in DEAL_KINDS:
        raise ValueError(f"kind must be one of {DEAL_KINDS}")
    key = str(deal_id or "").strip()
    if not key:
        raise ValueError("deal_id is required")
    return _link("child_deals", "deal_id", key, str(famly_child_id), {"kind": kind})


def set_kind(deal_id: str, kind: str) -> bool:
    """Change the type of a deal that is already linked (never moves it to another child). False if the
    deal is not in the registry."""
    if kind not in DEAL_KINDS:
        raise ValueError(f"kind must be one of {DEAL_KINDS}")
    with _open() as connection:
        cursor = connection.execute("UPDATE child_deals SET kind = ? WHERE deal_id = ?", (kind, str(deal_id).strip()))
        return bool(cursor.rowcount)


def add_email(famly_child_id: str, email: str) -> str:
    """Record a parent email on the child (one email may be on several children: siblings)."""
    key = str(email or "").strip().casefold()
    if not key:
        raise ValueError("email is required")
    with _open() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO child_emails (email, famly_child_id, created_at) VALUES (?, ?, ?)",
            (key, str(famly_child_id), _now()))
        return ADDED if cursor.rowcount else ALREADY


def set_decision(deal_id: str, famly_child_id: str, decision: str) -> None:
    """Record a person's answer to the same-child question. A later answer replaces an earlier one."""
    if decision not in (SAME, DIFFERENT):
        raise ValueError("decision must be same or different")
    deal, child = str(deal_id or "").strip(), str(famly_child_id or "").strip()
    if not deal or not child:
        raise ValueError("deal_id and famly_child_id are required")
    with _open() as connection:
        connection.execute(
            "INSERT INTO same_child_decisions (deal_id, famly_child_id, decision, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (deal_id, famly_child_id) DO UPDATE SET decision = excluded.decision, "
            "created_at = excluded.created_at", (deal, child, decision, _now()))


def add_sibling(famly_child_id: str, other_child_id: str, deal_id: str) -> str:
    """Record two children as siblings (the pair in id order, so A-B and B-A are one row): added | already."""
    a, b = sorted((str(famly_child_id or "").strip(), str(other_child_id or "").strip()))
    if not a or not b or a == b:
        raise ValueError("two different child ids are required")
    with _open() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO sibling_links (child_a, child_b, deal_id, created_at) VALUES (?, ?, ?, ?)",
            (a, b, str(deal_id), _now()))
        return ADDED if cursor.rowcount else ALREADY


def siblings(famly_child_id: str) -> list[str]:
    """The child ids recorded as siblings of this child."""
    key = str(famly_child_id or "").strip()
    with _open() as connection:
        rows = connection.execute(
            "SELECT child_a, child_b FROM sibling_links WHERE child_a = ? OR child_b = ?", (key, key)).fetchall()
    return sorted(r["child_b"] if r["child_a"] == key else r["child_a"] for r in rows)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _child(connection, child_id: str) -> Child | None:
    row = connection.execute("SELECT * FROM children WHERE famly_child_id = ?", (child_id,)).fetchone()
    if not row:
        return None
    urns = [r["urn"] for r in connection.execute(
        "SELECT urn FROM child_urns WHERE famly_child_id = ? ORDER BY urn", (child_id,))]
    deals = [(r["deal_id"], r["kind"]) for r in connection.execute(
        "SELECT deal_id, kind FROM child_deals WHERE famly_child_id = ? ORDER BY kind, deal_id", (child_id,))]
    return Child(famly_child_id=row["famly_child_id"], full_name=row["full_name"] or "", dob=row["dob"] or "",
                 site_code=row["site_code"], source=row["source"], urns=urns, deals=deals,
                 created_at=row["created_at"], updated_at=row["updated_at"])


def by_child_id(famly_child_id: str) -> Child | None:
    with _open() as connection:
        return _child(connection, str(famly_child_id).strip())


def _via(table: str, column: str, key: str) -> list[Child]:
    with _open() as connection:
        ids = [r["famly_child_id"] for r in connection.execute(
            f"SELECT DISTINCT famly_child_id FROM {table} WHERE {column} = ?", (key,))]
        return [c for c in (_child(connection, i) for i in ids) if c]


def by_urn(urn: str) -> Child | None:
    found = _via("child_urns", "urn", _urn_key(urn))
    return found[0] if found else None


def by_deal(deal_id: str) -> Child | None:
    found = _via("child_deals", "deal_id", str(deal_id).strip())
    return found[0] if found else None


def by_email(email: str) -> list[Child]:
    return _via("child_emails", "email", str(email or "").strip().casefold())


def by_dob(dob: str) -> list[Child]:
    """Every child with this date of birth (the name comparison is match.py's job)."""
    with _open() as connection:
        ids = [r["famly_child_id"] for r in connection.execute(
            "SELECT famly_child_id FROM children WHERE dob = ? ORDER BY famly_child_id", (str(dob or "")[:10],))]
        return [c for c in (_child(connection, i) for i in ids) if c]


def decisions_for_deal(deal_id: str) -> dict[str, str]:
    """{Famly child id: same | different} for the deal's answered same-child questions."""
    with _open() as connection:
        return {r["famly_child_id"]: r["decision"] for r in connection.execute(
            "SELECT famly_child_id, decision FROM same_child_decisions WHERE deal_id = ?", (str(deal_id).strip(),))}


def child_ids_by_site() -> dict[str, str | None]:
    """{Famly child id: site code} for every registry child (ids and codes only)."""
    with _open() as connection:
        return {r["famly_child_id"]: r["site_code"] for r in connection.execute(
            "SELECT famly_child_id, site_code FROM children")}


def stats() -> dict:
    """Counts only (no names)."""
    with _open() as connection:
        def one(sql):
            return connection.execute(sql).fetchone()[0]
        return {
            "children": one("SELECT COUNT(*) FROM children"),
            "without_dob": one("SELECT COUNT(*) FROM children WHERE dob = '' OR dob IS NULL"),
            "without_urn": one("SELECT COUNT(*) FROM children c WHERE NOT EXISTS "
                               "(SELECT 1 FROM child_urns u WHERE u.famly_child_id = c.famly_child_id)"),
            "without_deal": one("SELECT COUNT(*) FROM children c WHERE NOT EXISTS "
                                "(SELECT 1 FROM child_deals d WHERE d.famly_child_id = c.famly_child_id)"),
            "urns": one("SELECT COUNT(*) FROM child_urns"),
            "operations_deals": one(f"SELECT COUNT(*) FROM child_deals WHERE kind = '{OPERATIONS}'"),
            "adhoc_deals": one(f"SELECT COUNT(*) FROM child_deals WHERE kind = '{ADHOC}'"),
            "weekend_school_deals": one(f"SELECT COUNT(*) FROM child_deals WHERE kind = '{WEEKEND_SCHOOL}'"),
            "emails": one("SELECT COUNT(*) FROM child_emails"),
        }


def _reset_for_tests() -> None:
    with _open() as connection:
        for table in ("children", "child_urns", "child_deals", "child_emails", "same_child_decisions"):
            connection.execute(f"DELETE FROM {table}")
