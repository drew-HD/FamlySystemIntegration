"""create-test-children: make ONE persistent TEST child at each site (HDCITY, HDCW, HDWEST) so commands
can be tried in Famly without touching a real child.

A narrow, deliberate exception to "never call a write against Famly outside tests", in the same spirit as
the probe commands: it may CREATE test children and nothing else. There is no update and no delete
anywhere in this package (no delete document exists here). It never runs from a web action, queue or cron.

Guards, all before the first write:
  1. `--confirm` is required.
  2. Every payload has isTestChild true, hard-coded; `assert_test_payload` runs inside the single create
     path and refuses a payload without it, or whose name is not "AATest ..." or whose externalId is not
     "AATEST-...".
  3. IDEMPOTENT: the child is looked up first by its URN reference (AATEST-<SITE>). Found = left alone and
     reported; found but its summary does not say isTestChild true = REFUSED (never touched). A re-run
     therefore never creates a duplicate.
  4. At most MAX_CREATES (3) creates, never retried; a rejection is reported, not repeated.
  5. Each site's institutionId and Hatchlings group come from the reference files; if either is missing
     that site is skipped, nothing is guessed.
After a create the child's internal summary is re-read and must say isTestChild true (reported, not fatal).
The ids are printed and written to var/test_children.json (gitignored) -- ids only, no names.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from actions.onboard_dry_run import derive
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError
from integrations import catalogue

HERE = Path(__file__).parent
CREATE_QUERY = HERE / "create_child.graphql"
LOOKUP_QUERY = HERE / "lookup_by_ref.graphql"
CREATE_OP = "SeedChildCreate"
LOOKUP_OP = "SeedChildByRef"

SITES = ("HDCITY", "HDCW", "HDWEST")
GROUP_TITLE = "Hatchlings"
MAX_CREATES = 3
NAME_PREFIX = "AATest"
EXTERNAL_PREFIX = "AATEST-"
BIRTHDAY = "2024-06-01"
FIRST_DAY = "2026-01-05"
LAST_DAY = "2030-08-31"
STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "test_children.json"


class SeedRefused(Exception):
    """A guard failed. Nothing further was written."""


@dataclass
class SeedResult:
    created: dict[str, str] = field(default_factory=dict)      # site code -> child id
    existing: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)      # site code -> reason code
    writes: int = 0


def external_id(site_code: str) -> str:
    return f"{EXTERNAL_PREFIX}{site_code}"


def make_child_input(site_code: str, site_id: str, group_id: str) -> dict:
    """One ChildInput. `isTestChild` is hard-coded true."""
    return {
        "name": f"{NAME_PREFIX} {site_code}",
        "birthday": BIRTHDAY,
        "currentGroupId": group_id,
        "records": [],
        "sensitiveRecords": [],
        "isTestChild": True,
        "externalId": external_id(site_code),
        "externalSystemsReferences": [{"foreignId": external_id(site_code), "system": "URN"}],
        "sitesRelation": [{"siteId": site_id, "firstDay": FIRST_DAY, "lastDay": LAST_DAY}],
    }


def assert_test_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("isTestChild") is not True:
        raise SeedRefused("refusing: payload does not have isTestChild true")
    if not str(payload.get("name", "")).startswith(NAME_PREFIX):
        raise SeedRefused("refusing: payload name is not an AATest name")
    if not str(payload.get("externalId", "")).startswith(EXTERNAL_PREFIX):
        raise SeedRefused("refusing: payload externalId is not an AATEST id")


def is_test_child(summary: Any) -> bool:
    values = _find_all(summary, "isTestChild")
    return bool(values) and all(v is True for v in values)


def _find_all(node: Any, key: str) -> list:
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(_find_all(v, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_all(item, key))
    return found


def resolve_site(site_code: str) -> tuple[str, str]:
    """(institution id, Hatchlings group id) or raise SeedRefused with a code."""
    entry = catalogue.load_sites().get(site_code)
    site_id = entry.get("institutionId") if isinstance(entry, dict) else None
    if not site_id:
        raise SeedRefused("site_id_missing")
    try:
        return site_id, derive.group_id_for(site_code, GROUP_TITLE)
    except derive.Hold:
        raise SeedRefused("group_missing") from None


class Seeder:
    """The ONLY create path."""

    def __init__(self, rest, public):
        self.rest, self.public = rest, public
        self.result = SeedResult()

    def lookup(self, site_code: str) -> list[str]:
        body = self.public.execute(
            query_path=LOOKUP_QUERY,
            variables={"foreignId": external_id(site_code), "system": "URN"},
            operation_name=LOOKUP_OP)
        rows = body["data"]["children"]["listByExternalSystemReference"]["result"]
        return [row["id"] for row in rows]

    def summary_is_test(self, child_id: str) -> bool:
        try:
            return is_test_child(self.rest.get(f"/v2/children/{child_id}/summary"))
        except RestHTTPError:
            return False

    def create(self, payload: dict) -> str | None:
        assert_test_payload(payload)
        if self.result.writes >= MAX_CREATES:
            raise SeedRefused(f"create cap ({MAX_CREATES}) reached")
        self.result.writes += 1                       # counted before the request: a rejection spends it too
        body = self.public.execute(query_path=CREATE_QUERY, variables={"children": [payload]},
                                   operation_name=CREATE_OP)
        return body["data"]["children"]["create"][0]["id"]


def run(confirm: bool, rest, public, out: Callable[[str], None] = print,
        store_path: Path | None = None) -> SeedResult:
    if not confirm:
        raise SeedRefused("refusing without --confirm; nothing was written")
    seeder = Seeder(rest, public)
    result = seeder.result
    out(f"CAPS: at most {MAX_CREATES} creates, no retries, no update, no delete")
    for code in SITES:
        try:
            site_id, group_id = resolve_site(code)
        except SeedRefused as exc:
            result.skipped[code] = str(exc)
            out(f"{code}: SKIPPED ({exc})")
            continue
        try:
            ids = seeder.lookup(code)
        except (GraphQLError, GraphQLHTTPError) as exc:
            result.skipped[code] = "lookup_failed"
            out(f"{code}: SKIPPED, lookup failed ({type(exc).__name__}); nothing created for it")
            continue
        if len(ids) > 1:
            result.skipped[code] = "ambiguous_existing"
            out(f"{code}: SKIPPED, {len(ids)} children carry {external_id(code)}: {', '.join(ids)}")
            continue
        if ids:
            if not seeder.summary_is_test(ids[0]):
                result.skipped[code] = "existing_not_test_child"
                out(f"{code}: REFUSED, {ids[0]} carries {external_id(code)} but is not a test child; untouched")
                continue
            result.existing[code] = ids[0]
            out(f"{code}: already exists ({ids[0]}), left alone")
            continue
        try:
            child_id = seeder.create(make_child_input(code, site_id, group_id))
        except SeedRefused as exc:
            result.skipped[code] = "cap_reached"
            out(f"{code}: STOPPED, {exc}")
            break
        except (GraphQLError, GraphQLHTTPError) as exc:
            result.skipped[code] = "create_rejected"
            out(f"{code}: create REJECTED ({type(exc).__name__}); not retried. A GraphQL error can "
                f"still have created the child, so re-run (the lookup finds it) before assuming it failed")
            continue
        result.created[code] = child_id
        verified = seeder.summary_is_test(child_id)
        out(f"{code}: created {child_id}; summary isTestChild true = {verified}"
            + ("" if verified else "  <-- CHECK THIS CHILD BY HAND"))
    _save({**result.existing, **result.created}, store_path or STORE_PATH, out)
    return result


def _save(ids: dict[str, str], path: Path, out) -> None:
    if not ids:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    known.update(ids)
    path.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
    out(f"ids written to {path} (ids only)")
