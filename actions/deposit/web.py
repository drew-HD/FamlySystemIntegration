"""Web handler for `deposit_enqueue`: the HubSpot workflow's webhook after the contract is signed.

    {"action": "deposit_enqueue", "dealId": <deal record id>}

ENQUEUE ONLY: no HubSpot or Famly call. The job waits until the child, its bill payer and its plans exist.
    200  queued | already_queued | already_done | refused_mode_off
    400  dealId missing or not digits only
    500  a genuine internal error (HubSpot retries)
Only ids and codes are logged.
"""

import logging

from actions.onboard.web import parse_deal_id

from . import config, store

logger = logging.getLogger(__name__)

ACTION_NAME = "deposit_enqueue"


def _data(outcome: str, job: store.Job | None = None, errors: list | None = None) -> dict:
    return {"outcome": outcome, "jobId": job.id if job else None, "status": job.status if job else None,
            "warnings": [], "errors": errors or []}


def handle(payload: dict) -> tuple[dict, int]:
    deal_id = parse_deal_id(payload.get("dealId") if isinstance(payload, dict) else None)
    if deal_id is None:
        logger.info("deposit webhook: outcome=bad_request")
        return _data("bad_request", errors=["dealId must be a deal id: digits only"]), 400
    try:
        if config.mode() == config.MODE_OFF:
            logger.info("deposit webhook: outcome=refused_mode_off deal=%s", deal_id)
            return _data("refused_mode_off"), 200
        result, job = store.enqueue(deal_id, source="webhook")
    except Exception as exc:  # noqa: BLE001 - a genuine fault: HubSpot should retry
        logger.error("deposit webhook: internal error %s", type(exc).__name__)
        return _data("internal_error", errors=["internal error"]), 500
    outcome = {store.CREATED: "queued", store.ALREADY_ACTIVE: "already_queued",
               store.ALREADY_DONE: "already_done"}[result]
    logger.info("deposit webhook: outcome=%s deal=%s job=%s", outcome, deal_id, job.id)
    return _data(outcome, job), 200
