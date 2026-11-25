"""The URN section of a NEW-child ad hoc plan. Uses the onboarding allocator (UrnService + the onboard store's
`allocate_urn`), so a number is never given twice across onboarding and ad hoc. Dry mode reserves nothing; live mode
reserves one number for the deal (the same one on every re-plan). The URN is NOT written to the HubSpot deal.

Statuses: allocated (live) | would_allocate (dry) | generation_off | site_unknown | unavailable. Anything but the
first two is a HOLD (`urn_generation_off`, `urn_site_unknown`, `urn_not_available`): a number is never guessed."""

import logging

from actions.onboard_dry_run import urn as urn_fmt

logger = logging.getLogger(__name__)


def urn_section(service, deal_id: str, site_code: str, live: bool) -> dict:
    if not urn_fmt.enabled():
        return {"status": "generation_off"}
    if urn_fmt.site_suffix(site_code) is None:
        return {"status": "site_unknown"}
    if not live:
        return {"status": "would_allocate"}
    try:
        return {"status": "allocated", "urn": service.allocate(str(deal_id), None, site_code)}
    except Exception as exc:  # noqa: BLE001 - nothing is reserved from a guess
        logger.error("adhoc: urn allocation hit %s", type(exc).__name__)
        return {"status": "unavailable"}
