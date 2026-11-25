"""What the ad hoc worker asks of the children registry (stages 2 and 3, docs/registry_design.md).

Reads: the registry children a deal's name + date of birth match (match.py rules), and a child's operations deals.
Writes (live mode only, after a confirmed Famly write): a new child's row, and an ad hoc deal on a child. A write
never raises: it returns a code (added | already | conflict | failed), because a registry problem is a flag, never a
stop. Names and dates of birth pass through memory only; nothing here logs them.
"""

import logging

from . import match, store

logger = logging.getLogger(__name__)

FAILED = "failed"


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


def record_new_child(child_id: str, name: str, dob: str, site_code: str | None, urn: str | None,
                     deal_id: str) -> str:
    """A child the ad hoc worker just created: its row, URN and ad hoc deal. The worst code wins."""
    try:
        store.upsert_child(child_id, name, dob, site_code, "adhoc")
        codes = [store.add_urn(child_id, urn)] if urn else []
        codes.append(store.add_deal(child_id, deal_id, store.ADHOC))
    except Exception as exc:  # noqa: BLE001 - a registry problem never stops the job
        logger.error("registry: new child write hit %s", type(exc).__name__)
        return FAILED
    return store.CONFLICT if store.CONFLICT in codes else store.ADDED


MISSING = "missing"


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
