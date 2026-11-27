"""What the ad hoc and onboarding workers ask of the children registry (stages 2-4, docs/registry_design.md).

Reads: the registry children a deal's name + date of birth match (match.py rules), and a child's operations deals.
Writes (live mode only, after a confirmed Famly write): a new child's row, and an ad hoc deal on a child. A write
never raises: it returns a code (added | already | conflict | failed), because a registry problem is a flag, never a
stop. Names and dates of birth pass through memory only; nothing here logs them.
"""

import logging

from . import match, store

logger = logging.getLogger(__name__)

FAILED = "failed"
MISSING = "missing"


def candidates(name: str, dob: str) -> list[tuple[str, str]]:
    """(exact | similar, Famly child id) for every registry child with this date of birth whose name matches.
    Raises if the registry cannot be read (the caller holds the job)."""
    if not name or not dob:
        return []
    return [(result, child.famly_child_id) for result, child in match.find(name, dob, store.by_dob(dob))]


def operations_deals(child_id: str) -> list[str]:
    """The child's operations deal ids in the registry. Raises if the registry cannot be read."""
    child = store.by_child_id(child_id)
    return [deal_id for deal_id, kind in (child.deals if child else []) if kind == store.OPERATIONS]


def operations_candidates(name: str, dob: str, deal_id: str) -> list[dict]:
    """Stage 4: the registry children an OPERATIONS deal's name + date of birth match, for the onboarding
    planner. Ids, codes and URNs only (never a name or date of birth):
      {childId, result (exact | similar), adhocOnly (an ad hoc deal and no operations deal),
       decision (same | different | None: a person's answer for THIS deal), urns, siteCode}.
    Raises if the registry cannot be read (the planner holds)."""
    if not name or not dob:
        return []
    decisions = store.decisions_for_deal(deal_id)
    found = []
    for result, child in match.find(name, dob, store.by_dob(dob)):
        kinds = {kind for _deal, kind in child.deals}
        found.append({
            "childId": child.famly_child_id, "result": result,
            "adhocOnly": store.ADHOC in kinds and store.OPERATIONS not in kinds,
            "decision": decisions.get(child.famly_child_id), "urns": list(child.urns),
            "siteCode": child.site_code,
        })
    return found


def child_name(child_id: str) -> str | None:
    """The registry name of a child, for the same-child question and the join summary in Slack (the Slack
    exception). Memory only; None if unknown or unreadable."""
    try:
        child = store.by_child_id(child_id)
    except Exception:  # noqa: BLE001
        return None
    return (child.full_name or None) if child else None


def record_decision(deal_id: str, child_id: str, same: bool) -> None:
    """A person's answer to the same-child question. Raises if the registry cannot be written."""
    store.set_decision(deal_id, child_id, store.SAME if same else store.DIFFERENT)


def record_siblings(child_id: str, other_child_ids, deal_id: str) -> str:
    """A person confirmed the child and the other children as siblings (Gate A): added | already | failed.
    Never raises."""
    try:
        codes = [store.add_sibling(child_id, other, deal_id) for other in other_child_ids if other != child_id]
    except Exception as exc:  # noqa: BLE001
        logger.error("registry: sibling write hit %s", type(exc).__name__)
        return FAILED
    return store.ADDED if store.ADDED in codes else store.ALREADY


def record_operations_deal(child_id: str, deal_id: str) -> str:
    """Stage 4: an operations deal joined (or found) a child already in the registry: added | already | conflict |
    missing | failed. Never raises."""
    try:
        if store.by_child_id(child_id) is None:
            return MISSING
        return store.add_deal(child_id, deal_id, store.OPERATIONS)
    except Exception as exc:  # noqa: BLE001
        logger.error("registry: operations deal write hit %s", type(exc).__name__)
        return FAILED


def record_new_child(child_id: str, name: str, dob: str, site_code: str | None, urn: str | None,
                     deal_id: str, kind: str = store.ADHOC) -> str:
    """A child a worker just created (ad hoc, or onboarding with kind=operations): its row, URN and deal. The
    worst code wins."""
    try:
        store.upsert_child(child_id, name, dob, site_code, kind)
        codes = [store.add_urn(child_id, urn)] if urn else []
        codes.append(store.add_deal(child_id, deal_id, kind))
    except Exception as exc:  # noqa: BLE001 - a registry problem never stops the job
        logger.error("registry: new child write hit %s", type(exc).__name__)
        return FAILED
    return store.CONFLICT if store.CONFLICT in codes else store.ADDED


def record_existing_child(child_id: str, details: dict | None, site_code: str | None, deal_id: str) -> str:
    """An ad hoc deal booked on an EXISTING child: added | already | conflict | missing | failed. `details` is the
    child's Famly name, date of birth and URN from the live roster (memory only): a child not in the registry yet
    (e.g. made by onboarding after the backfill) gets its row from it. Without details, a child that is not in the
    registry is `missing` (nothing is written)."""
    try:
        if details:
            store.upsert_child(child_id, details.get("name") or "", details.get("dob") or "", site_code, "adhoc")
            codes = [store.add_urn(child_id, details["urn"])] if details.get("urn") else []
        elif store.by_child_id(child_id) is None:
            return MISSING
        else:
            codes = []
        codes.append(store.add_deal(child_id, deal_id, store.ADHOC))
    except Exception as exc:  # noqa: BLE001
        logger.error("registry: deal write hit %s", type(exc).__name__)
        return FAILED
    if store.CONFLICT in codes:
        return store.CONFLICT
    return codes[-1]
