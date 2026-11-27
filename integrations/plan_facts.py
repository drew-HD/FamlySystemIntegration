"""The facts the plan approval message shows that are not in the previewed plan itself.

  * the fee option: the deal's HubSpot `full_funded` property ("Additional Fees Options"), read by deal id when
    the message is built. Its stored VALUES are older names; what each one means (Drew, 2026-11-27):
        Full Fees              -> Full fees (meals and activities fees)
        Term only              -> Fully funded (funded hours only, no extras)
        Meals Only             -> Activities only (no meals)
        Enrichment Extras Only -> Meals only (no educational activities and extras)
        No Additional Fees     -> No extras
    Any other value is shown by its own text.
  * the room: the child's current group, from Famly's child summary, titled from
    reference/groups_catalogue.json.
  * term time / full year, the funding entitlement and the advance fee period, from the plan's payload.

Every lookup is best effort: a failure shows "unknown" and never stops the message. Nothing is stored or logged
except the exception type.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEE_OPTION_PROPERTY = "full_funded"
FEE_LABELS = {
    "full fees": "Full fees",
    "term only": "Fully funded",
    "meals only": "Activities only (no meals)",
    "enrichment extras only": "Meals only (no educational activities and extras)",
    "no additional fees": "No extras",
}
SUMMARY_PATH = "/v2/children/{child_id}/summary"
LOOKUP_TIMEOUT_SECONDS = 5
# Live lookups (with no client passed in). The test package turns this off suite-wide (tests/__init__.py);
# a test of the lookups passes its own fake client, which always runs.
LIVE_LOOKUPS = True


def fee_label(value: Any) -> str | None:
    """The plain label for a `full_funded` value; an unknown value as it is; None when empty."""
    text = str(value or "").strip()
    if not text:
        return None
    return FEE_LABELS.get(text.casefold(), text)


def fee_option(deal_id: Any, client: Any = None) -> str | None:
    """The deal's fee option as a plain label, or None (no deal id, empty, or HubSpot could not be read)."""
    deal = "".join(ch for ch in str(deal_id or "") if ch.isdigit())
    if not deal or (client is None and not LIVE_LOOKUPS):
        return None
    try:
        if client is None:
            from core.hubspot import HubSpotClient
            client = HubSpotClient(sleep=lambda _s: None)
        return fee_label(client.read_deal_property(deal, FEE_OPTION_PROPERTY))
    except Exception as exc:  # noqa: BLE001 - a missing label never stops the message
        logger.warning("plan facts: fee option not read (%s)", type(exc).__name__)
        return None


def _groups_path() -> Path:
    override = os.environ.get("GROUPS_CATALOGUE_FILE", "").strip()
    if override:
        return Path(override)
    from integrations import reference_files
    return reference_files.path_for("groups_catalogue")


def group_title(site_code: Any, group_id: Any) -> str | None:
    try:
        data = json.loads(_groups_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    institutions = data.get("institutions") if isinstance(data, dict) else None
    site = (institutions or {}).get(str(site_code or "").strip().upper()) or {}
    groups = site.get("groups") if isinstance(site, dict) else None
    title = (groups or {}).get(str(group_id)) if isinstance(groups, dict) else None
    if isinstance(title, dict):
        title = title.get("title")
    return str(title) if title else None


def room(child_id: Any, site_code: Any, rest: Any = None) -> str | None:
    """The child's current room (group title), or None."""
    if not child_id:
        return None
    if rest is None and (not LIVE_LOOKUPS or not os.environ.get("FAMLY_ACCESS_TOKEN", "").strip()):
        return None                                  # no token, no lookup (as the child name lookup does)
    try:
        if rest is None:
            from core.rest_client import RestClient
            rest = RestClient(timeout=LOOKUP_TIMEOUT_SECONDS)
        from actions.onboard.summary_check import _group_id
        group_id = _group_id(rest.get(SUMMARY_PATH.format(child_id=child_id)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan facts: room not read (%s)", type(exc).__name__)
        return None
    return group_title(site_code, group_id) if group_id else None


def _flag(value: Any) -> bool | None:
    text = str(value).strip().lower() if value is not None else ""
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def facts_for(payload: dict | None, child_id: Any, site_code: Any, *, deal_id: Any = None, index: int = 0,
              total: int = 1, advance: tuple | None = None, notes=(), span: tuple | None = None,
              hubspot: Any = None, rest: Any = None) -> dict:
    """The `facts` for `slack.build_summary` from a plan's flat payload (one segment's, for a timeline)."""
    payload = payload or {}
    deal_id = deal_id or payload.get("dealId")
    funded = _flag(payload.get("funded"))
    entitlement = str(payload.get("funding_entitlement") or "").strip()
    return {
        "index": index,
        "total": max(total, 1),
        "termTimeOnly": _flag(payload.get("termTimeOnly")),
        "fees": fee_option(deal_id, hubspot),
        "funding": entitlement if funded and entitlement else None,
        "advance": advance,
        "room": room(child_id, site_code, rest),
        "notes": list(notes),
        "span": span,
    }
