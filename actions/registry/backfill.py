"""registry-backfill: fill the children registry from Famly and HubSpot. READ-ONLY against both.

Sources:
  1. Famly, every child at every site, past and future (onboard_audit.fetch_children): id, name, date of
     birth, URN (externalId), HUBSPOT deal references and parent emails. Every Famly child gets a row, so
     the registry catches everyone.
  2. HubSpot, every deal in the Operations and Ad Hoc pipelines: the deal kind, plus its `famly_childid`
     and `child_urn`.
  3. The local job stores, opened read-only: done onboarding and ad hoc jobs (deal -> child) and the URN
     allocations (deal -> URN -> child).

A deal is linked when every source agrees on ONE child. A deal that points at several children, or a URN
that is on several children, is a conflict: reported, never written. Dry run by default; `confirm=True`
writes. Output is counts and codes; the CSV holds ids and codes only.
"""

import csv
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import store

logger = logging.getLogger(__name__)

# Codes (ids only in the report).
DEAL_SEVERAL_CHILDREN = "deal_points_at_several_children"
URN_SEVERAL_CHILDREN = "urn_on_several_children"
DEAL_NO_CHILD = "deal_no_child_found"
REFERENCE_UNKNOWN_DEAL = "reference_not_operations_or_adhoc"
STORE_CONFLICT = "already_tied_to_another_child"
CHILD_NO_DOB = "child_without_dob"
RECORDED_CHILD_MISSING = "recorded_child_not_in_famly"

# Deals with no Famly child in these stages are left out of the --deals-out list (they will not be run again).
CLOSED_STAGES = {"left nursery", "ad hoc completed", "closed lost"}


@dataclass
class Report:
    rows: list = field(default_factory=list)             # (code, child_id, deal_id)
    counts: Counter = field(default_factory=Counter)

    def add(self, code: str, child_id: str = "", deal_id: str = "") -> None:
        self.rows.append((code, child_id, deal_id))
        self.counts[code] += 1


def _read_local(path: Path, sql: str) -> list[tuple]:
    """Rows from another store, opened READ-ONLY. Missing file or table -> no rows."""
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return []
    try:
        return [tuple(r) for r in connection.execute(sql)]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def local_links() -> dict:
    """deal -> child from done jobs, and deal -> URN from the allocations, out of the local stores."""
    from actions.adhoc import store as adhoc_store
    from actions.onboard import store as onboard_store
    from actions.onboard_dry_run import urn as urn_fmt

    adhoc = _read_local(adhoc_store.store_path(),
                        "SELECT deal_id, child_id FROM adhoc_jobs WHERE status = 'done' AND child_id IS NOT NULL")
    onboard = _read_local(onboard_store.store_path(),
                          "SELECT deal_id, child_id FROM onboard_jobs WHERE status = 'done' AND child_id IS NOT NULL")
    allocations = []
    for deal_id, suffix, number in _read_local(onboard_store.store_path(),
                                               "SELECT deal_id, site_code, number FROM onboard_urn_allocations"):
        try:
            allocations.append((str(deal_id), urn_fmt.format_urn(suffix, int(number))))
        except (TypeError, ValueError):
            continue
    return {"adhoc_jobs": [(str(d), str(c)) for d, c in adhoc],
            "onboard_jobs": [(str(d), str(c)) for d, c in onboard],
            "urn_allocations": allocations}


def _site_codes() -> dict:
    """Famly institution id -> site code, from reference/catalogue.json."""
    from integrations import catalogue
    return {entry["institutionId"]: code for code, entry in catalogue.load_sites().items()
            if isinstance(entry, dict) and entry.get("institutionId")}


def _site_code(child, site_codes: dict) -> str | None:
    relations = sorted((r for r in child.relations if r and r[0]), key=lambda r: r[1] or "")
    return site_codes.get(relations[-1][0]) if relations else None


WEEKEND_SCHOOL_PIPELINE = "weekend school"


def weekend_school_deals(hubspot, stages: dict) -> list[dict]:
    """Every deal in the Weekend School pipeline: one search per page of 100."""
    from actions.onboard_dry_run import derive
    from core import hubspot as hubspot_cfg
    ids = [sid for sid, (pipeline, _) in stages.items() if derive.norm_stage(pipeline) == WEEKEND_SCHOOL_PIPELINE]
    if not ids:
        return []
    return hubspot.search_deals([{"propertyName": "dealstage", "operator": "IN", "values": ids}],
                                list(hubspot_cfg.DEAL_PROPERTIES))


def build(children: dict, operations_deals: list[dict], adhoc_deals: list[dict], links: dict,
          site_codes: dict, stages: dict | None = None,
          weekend_deals: list[dict] | None = None) -> tuple[dict, Report]:
    """Work out what to write, without writing. Returns ({"children": [...], "deals": [(deal, child,
    kind)]}, report)."""
    report = Report()
    from actions.onboard_dry_run import derive
    stage_of = {str(d["id"]): (stages or {}).get(str(d.get("dealstage")), ("", ""))[1]
                for d in operations_deals + adhoc_deals + (weekend_deals or [])}
    kinds = {str(d["id"]): store.OPERATIONS for d in operations_deals}
    kinds.update({str(d["id"]): store.ADHOC for d in adhoc_deals})
    kinds.update({str(d["id"]): store.WEEKEND_SCHOOL for d in weekend_deals or []})

    by_urn: dict[str, list[str]] = {}
    for child in children.values():
        if child.external_id:
            by_urn.setdefault(child.external_id.casefold(), []).append(child.id)

    def urn_children(urn: str) -> list[str]:
        return by_urn.get(str(urn or "").strip().casefold(), []) if urn else []

    candidates: dict[str, set] = {}
    broken: set = set()          # a source names a child that is not in Famly: the deal is not linked

    def offer(deal_id: str, child_id: str) -> None:
        if not deal_id or not child_id:
            return
        if child_id not in children:
            if deal_id not in broken:
                report.add(RECORDED_CHILD_MISSING, "", deal_id)
            broken.add(deal_id)
            return
        candidates.setdefault(deal_id, set()).add(child_id)

    rows = []
    for child in children.values():
        if not child.birthday:
            report.add(CHILD_NO_DOB, child.id)
        if child.external_id and len(urn_children(child.external_id)) > 1:
            report.add(URN_SEVERAL_CHILDREN, child.id)
        rows.append(child)
        for system, foreign in child.references:
            if system != "HUBSPOT" or not foreign:
                continue
            if foreign in kinds:
                offer(foreign, child.id)
            else:
                report.add(REFERENCE_UNKNOWN_DEAL, child.id, foreign)

    for deal in operations_deals + adhoc_deals + (weekend_deals or []):
        deal_id = str(deal["id"])
        recorded = str(deal.get("famly_childid") or "").strip()
        if recorded:
            offer(deal_id, recorded)
        found = urn_children(str(deal.get("child_urn") or "").strip())
        if len(found) == 1:
            offer(deal_id, found[0])
    for deal_id, child_id in links.get("adhoc_jobs", []):
        offer(deal_id, child_id)
        kinds.setdefault(deal_id, store.ADHOC)
    for deal_id, child_id in links.get("onboard_jobs", []):
        offer(deal_id, child_id)
        kinds.setdefault(deal_id, store.OPERATIONS)
    for deal_id, urn in links.get("urn_allocations", []):
        found = urn_children(urn)
        if len(found) == 1 and deal_id in kinds:
            offer(deal_id, found[0])

    deals, no_child = [], []
    for deal_id, kind in sorted(kinds.items()):
        ids = sorted(candidates.get(deal_id, ()))
        if deal_id in broken:
            continue
        if len(ids) == 1:
            deals.append((deal_id, ids[0], kind))
        elif ids:
            for child_id in ids:
                report.add(DEAL_SEVERAL_CHILDREN, child_id, deal_id)
        else:
            report.add(DEAL_NO_CHILD, "", deal_id)
            report.counts[f"{DEAL_NO_CHILD}:{kind}"] += 1
            if deal_id in stage_of and derive.norm_stage(stage_of[deal_id]) not in CLOSED_STAGES:
                no_child.append((deal_id, kind, stage_of[deal_id]))
    shared_urns = {urn for urn, ids in by_urn.items() if len(ids) > 1}
    closed = {deal_id for deal_id, stage in stage_of.items() if derive.norm_stage(stage) in CLOSED_STAGES}
    return {"children": rows, "deals": deals, "site_codes": site_codes, "shared_urns": shared_urns,
            "no_child_deals": no_child, "closed_deals": closed,
            "child_deals": _by_child(candidates),
            "deal_names": deal_child_names(operations_deals + adhoc_deals + (weekend_deals or []))}, report


def _by_child(candidates: dict) -> dict:
    """child id -> every deal any source tied to it this run (linked or not)."""
    out: dict = {}
    for deal_id, child_ids in candidates.items():
        for child_id in child_ids:
            out.setdefault(child_id, set()).add(deal_id)
    return out


def deal_child_names(deals: list[dict]) -> dict:
    """deal id -> the child's name on the deal (full name, else first + last). In memory only: the refresh shows
    it in Slack and reports/ for a deal whose child is not in Famly. Never logged."""
    from core import hubspot as hubspot_cfg
    names = {}
    for deal in deals:
        name = str(deal.get(hubspot_cfg.CHILD_FULL_NAME_PROPERTY) or "").strip() or " ".join(
            str(deal.get(p) or "").strip() for p in hubspot_cfg.CHILD_NAME_PROPERTIES)
        if name.strip():
            names[str(deal["id"])] = " ".join(name.split())
    return names


def write(plan: dict, report: Report) -> Counter:
    """Write the plan to the registry. A URN or deal already tied to another child is reported, not changed."""
    written = Counter()
    for child in plan["children"]:
        if store.upsert_child(child.id, child.full_name, child.birthday, _site_code(child, plan["site_codes"]),
                              "backfill"):
            written["children_added"] += 1
        else:
            written["children_refreshed"] += 1
        if child.external_id and child.external_id.casefold() not in plan["shared_urns"]:
            result = store.add_urn(child.id, child.external_id)
            written[f"urn_{result}"] += 1
            if result == store.CONFLICT:
                report.add(STORE_CONFLICT, child.id)
        for contact in child.contacts:
            if contact.email:
                store.add_email(child.id, contact.email)
    for deal_id, child_id, kind in plan["deals"]:
        result = store.add_deal(child_id, deal_id, kind)
        written[f"deal_{result}"] += 1
        if result == store.CONFLICT:
            report.add(STORE_CONFLICT, child_id, deal_id)
    return written


def run(hubspot, gql, *, confirm: bool = False, site_ids: list[str] | None = None, children=None,
        links: dict | None = None, site_codes: dict | None = None) -> tuple[dict, Report]:
    """The backfill. `children`, `links`, `site_codes` are for tests."""
    from actions.adhoc_audit import runner as adhoc_audit
    from actions.onboard_audit import runner as audit

    if children is None:
        if site_ids is None:
            from actions.urn_lookup import runner as urn_runner
            site_ids = urn_runner._all_institution_ids()
        children = audit.fetch_children(gql, site_ids)
    stages = hubspot.stage_names()
    operations = audit.operations_deals(hubspot, stages, None)
    adhoc = adhoc_audit.adhoc_deals(hubspot, stages)
    weekend = weekend_school_deals(hubspot, stages)
    links = local_links() if links is None else links
    site_codes = _site_codes() if site_codes is None else site_codes

    plan, report = build(children, operations, adhoc, links, site_codes, stages, weekend)
    kinds = Counter(kind for _, _, kind in plan["deals"])
    summary = {
        "mode": "confirm" if confirm else "dry_run",
        "famly_children": len(plan["children"]),
        "children_with_urn": sum(1 for c in plan["children"] if c.external_id),
        "operations_deals": len(operations),
        "adhoc_deals": len(adhoc),
        "weekend_school_deals": len(weekend),
        "deals_to_link": dict(kinds),
        "codes": dict(sorted(report.counts.items())),
        "live_deals_without_child": dict(Counter(kind for _, kind, _ in plan["no_child_deals"])),
    }
    if confirm:
        summary["written"] = dict(sorted(write(plan, report).items()))
        summary["codes"] = dict(sorted(report.counts.items()))
        summary["registry"] = store.stats()
    logger.info("registry backfill %s: %s children, %s deals linked", summary["mode"],
                summary["famly_children"], sum(kinds.values()))
    summary["_no_child_deals"] = plan["no_child_deals"]
    summary["_closed_deals"] = plan["closed_deals"]          # the refresh uses these three; the CLI drops them
    summary["_child_deals"] = plan["child_deals"]
    summary["_deal_names"] = plan["deal_names"]
    return summary, report


def write_deal_list(path: str, no_child_deals: list) -> int:
    """Deal ids with no Famly child (closed stages left out), one per line. Ids only; NOT the registry."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{deal_id}\n" for deal_id, _, _ in no_child_deals), encoding="utf-8")
    return len(no_child_deals)


def write_csv(path: str, report: Report) -> int:
    """Ids and codes only."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "famly_child_id", "deal_id"])
        writer.writerows(report.rows)
    return len(report.rows)
