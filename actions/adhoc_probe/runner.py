"""adhoc-probe-read: READ-ONLY probe of Famly's INTERNAL endpoints for ad hoc bookings, on the TEST
children only (ids from var/test_children.json, written by create-test-children).

Stage 1 of the probe in docs/adhoc_design.md. It answers, without writing anything:
  * can our token call the internal sessions/products lookup and the conflict preview?
  * what sessions, weekday availabilities and pricing groups/prices does a child see?
  * what tags exist (and is the HDCW ad hoc tag among them)?
  * which pricing-related fields does a test child's summary carry?

Guards: Famly GraphQL goes through ReadOnlyGraphQL (refuses any document containing a mutation),
REST through ReadOnlyRest (get only). A child id is used only if it is in the test-children store AND its
summary says isTestChild true. The raw responses are saved under reports/ (gitignored). The write stage
(tag, end date, booking) is a separate, later change.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from actions.onboard_audit import runner as audit
from actions.testchild_seed import runner as seed

HERE = Path(__file__).parent
SESSIONS_QUERY = HERE / "sessions_and_products.graphql"
CONFLICTS_QUERY = HERE / "booking_conflicts.graphql"
SESSIONS_OP = "GetSessionsAndProducts"
CONFLICTS_OP = "GetAdHocPurchaseSessionsConflicts"
TAG_PATHS = ("/v2/tags",)
ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports"


class ProbeRefused(Exception):
    pass


def first_monday_after(today: date, days: int = 30) -> date:
    day = today + timedelta(days=days)
    return day + timedelta(days=(7 - day.weekday()) % 7)


def load_test_children(path: Path | None = None) -> dict[str, str]:
    try:
        data = json.loads((path or seed.STORE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ProbeRefused("no test children store; run create-test-children first") from None
    ids = {k: v for k, v in data.items() if k in seed.SITES and isinstance(v, str)}
    if not ids:
        raise ProbeRefused("the test children store is empty")
    return ids


def verify_test_children(rest, ids: dict[str, str]) -> dict[str, str]:
    """Only children whose internal summary says isTestChild true are kept; others are refused."""
    ok = {}
    for site, child_id in ids.items():
        try:
            if seed.is_test_child(rest.get(f"/v2/children/{child_id}/summary")):
                ok[site] = child_id
        except Exception:  # noqa: BLE001 - an unreadable summary means NOT verified
            pass
    if not ok:
        raise ProbeRefused("no test child could be verified (isTestChild); nothing was read")
    return ok


def describe_sessions(body: dict) -> list[str]:
    """One line per session/product version: title, type, days, and price per pricing group."""
    items = (((body or {}).get("data") or {}).get("finance") or {}).get("revenueItems") or {}
    lines = []
    for kind, key, avail in (("session", "sessionVersions", "sessionAvailabilities"),
                             ("product", "productVersions", "productAvailabilities")):
        for v in items.get(key) or []:
            days = sorted({a.get("day") for a in v.get(avail) or [] if a.get("day")})
            prices = ", ".join(f"{p.get('pricingGroupId')}={p.get('adHocPrice')}/{p.get('period')}"
                               for p in v.get("pricing") or [])
            lines.append(f"  {kind}: {v.get('title')!r} type={v.get('type', '-')} days={','.join(days) or '-'} "
                         f"validFrom={v.get('validFrom')} prices[{prices}]")
    return lines


def pricing_fields(node: Any, path: str = "") -> list[str]:
    """Summary keys whose name mentions pricing, with their values (test children only)."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if "pric" in str(k).lower():
                found.append(f"{here} = {json.dumps(v)[:200]}")
            found.extend(pricing_fields(v, here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(pricing_fields(item, f"{path}[{i}]"))
    return found


def first_session_candidate(body: dict, day_name: str = "MONDAY") -> dict | None:
    """A preview-only purchase input from the first session that has a `day_name` availability and a
    price. Used ONLY for the conflict PREVIEW; nothing is booked."""
    items = (((body or {}).get("data") or {}).get("finance") or {}).get("revenueItems") or {}
    for v in items.get("sessionVersions") or []:
        avail = next((a for a in v.get("sessionAvailabilities") or [] if a.get("day") == day_name), None)
        price = next((p for p in v.get("pricing") or [] if p.get("adHocPrice") and p.get("pricingGroupId")), None)
        if avail and price:
            return {"title": v.get("title"), "sessionAvailabilityId": avail["sessionAvailabilityId"],
                    "pricingGroupId": price["pricingGroupId"], "bookedPrice": price["adHocPrice"]}
    return None


def run_reads(rest, internal, out: Callable[[str], None] = print, today: date | None = None,
              store_path: Path | None = None, report_dir: Path | None = None) -> dict:
    today = today or date.today()
    rest = audit.ReadOnlyRest(rest)
    gql = audit.ReadOnlyGraphQL(internal)
    children = verify_test_children(rest, load_test_children(store_path))
    valid_on = first_monday_after(today)
    out(f"TEST CHILDREN verified: {', '.join(f'{s}={c}' for s, c in children.items())}")
    out(f"lookup date {valid_on} ({valid_on.strftime('%A')}); READ-ONLY, nothing is written")
    raw: dict[str, Any] = {"validOn": valid_on.isoformat()}

    for site, child_id in children.items():
        out(f"=== {site} {child_id} ===")
        try:
            summary = rest.get(f"/v2/children/{child_id}/summary")
            raw[f"{site}.summary"] = summary
            fields = pricing_fields(summary)
            out("  summary pricing-related fields: " + (" | ".join(fields) if fields else "none found"))
        except Exception as exc:  # noqa: BLE001
            out(f"  summary FAILED: {type(exc).__name__}")
        try:
            body = gql.execute(query_path=SESSIONS_QUERY,
                               variables={"childIds": [child_id], "validOnDate": valid_on.isoformat()},
                               operation_name=SESSIONS_OP)
        except Exception as exc:  # noqa: BLE001
            out(f"  {SESSIONS_OP} FAILED: {_why(exc)}")
            continue
        raw[f"{site}.sessions"] = body
        lines = describe_sessions(body)
        out(f"  {SESSIONS_OP}: {len(lines)} item(s)")
        for line in lines:
            out(line)
        for label, purchases in (("empty purchases", []), ("one candidate session (PREVIEW only)", None)):
            if purchases is None:
                cand = first_session_candidate(body)
                if not cand:
                    out("  no session with a Monday availability and price to preview")
                    continue
                purchases = [{"childId": child_id, "discountAmount": "0", "productBookings": [],
                              "sessionBookings": [{"bookedPrice": cand["bookedPrice"],
                                                   "pricingGroupId": cand["pricingGroupId"],
                                                   "purchaseDate": valid_on.isoformat(),
                                                   "sessionAvailabilityId": cand["sessionAvailabilityId"]}]}]
                label += f" ({cand['title']!r})"
            try:
                preview = gql.execute(query_path=CONFLICTS_QUERY,
                                      variables={"childId": child_id, "purchases": purchases,
                                                 "date": valid_on.isoformat()},
                                      operation_name=CONFLICTS_OP)
                raw[f"{site}.conflicts.{len(purchases)}"] = preview
                out(f"  {CONFLICTS_OP} [{label}]: {json.dumps(preview.get('data'))[:600]}")
            except Exception as exc:  # noqa: BLE001
                out(f"  {CONFLICTS_OP} [{label}] FAILED: {_why(exc)}")

    out("=== TAGS (GET only) ===")
    for path in TAG_PATHS:
        try:
            tags = rest.get(path)
            raw["tags"] = tags
            rows = tags if isinstance(tags, list) else (tags or {}).get("tags") if isinstance(tags, dict) else None
            if isinstance(rows, list):
                out(f"  GET {path}: {len(rows)} tag(s)")
                for t in rows:
                    if isinstance(t, dict):
                        out("   " + json.dumps({k: t.get(k) for k in ("id", "title", "name", "institutionId",
                                                                       "type") if k in t}))
            else:
                out(f"  GET {path}: {json.dumps(tags)[:800]}")
        except Exception as exc:  # noqa: BLE001
            out(f"  GET {path} FAILED: {_why(exc)}")

    directory = report_dir or REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"adhoc_probe_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    target.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    out(f"raw responses saved to {target} (gitignored; test children only)")
    return {"children": children, "requests": {"graphql": gql.calls, "rest": rest.calls}, "report": str(target)}


def _why(exc: Exception) -> str:
    body = getattr(exc, "errors", None) or getattr(exc, "body", None) or str(exc)
    return f"{type(exc).__name__}: {json.dumps(body, default=str)[:700]}"
