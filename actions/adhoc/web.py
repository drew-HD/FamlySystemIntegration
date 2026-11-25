"""Web handler for the `adhoc_enqueue` action: the HTTP trigger for the ad hoc worker.

Dispatched by the intake route like every other action. Body:

    {"action": "adhoc_enqueue", "dealId": <deal record id>}

`dealId` may be a JSON number or a string; digits only (ASCII 0-9), else 400.

THIS DOES NO WORK: it calls `store.enqueue(deal_id, mode, source="webhook")` and returns. No HubSpot or
Famly call. Status codes (HubSpot retries on 5xx, so 5xx means a real fault only):

    200  queued | already_queued | already_done     (data.jobId set)
    200  refused_mode_off     ADHOC_MODE is off      (no job)
    400  dealId missing or not digits only
    500  a genuine internal error

Only ids and codes are logged. An exception is logged by type only.
"""

import logging

from actions.onboard.web import parse_deal_id

from . import config, store

logger = logging.getLogger(__name__)

ACTION_NAME = "adhoc_enqueue"
SOURCE = "webhook"

OUTCOME_QUEUED = "queued"
OUTCOME_ALREADY_QUEUED = "already_queued"
OUTCOME_ALREADY_DONE = "already_done"
OUTCOME_MODE_OFF = "refused_mode_off"


def _data(outcome: str, job: store.Job | None = None) -> dict:
    return {"outcome": outcome, "jobId": job.id if job else None, "status": job.status if job else None,
            "warnings": [], "errors": []}


def handle(payload: dict) -> tuple[dict, int]:
    deal_id = parse_deal_id(payload.get("dealId") if isinstance(payload, dict) else None)
    if deal_id is None:
        data = _data("bad_request")
        data["errors"] = ["dealId must be a deal id: digits only"]
        logger.info("adhoc webhook: outcome=bad_request")
        return data, 400
    try:
        mode = config.mode()
        if mode == config.MODE_OFF:
            logger.info("adhoc webhook: outcome=%s deal=%s", OUTCOME_MODE_OFF, deal_id)
            return _data(OUTCOME_MODE_OFF), 200
        result = store.enqueue(deal_id, mode, source=SOURCE)
    except Exception as exc:  # noqa: BLE001 - a genuine fault: HubSpot should retry
        logger.error("adhoc webhook: internal error %s", type(exc).__name__)
        data = _data("internal_error")
        data["errors"] = ["internal error"]
        return data, 500
    outcome = {store.CREATED: OUTCOME_QUEUED, store.ALREADY_QUEUED: OUTCOME_ALREADY_QUEUED,
               store.ALREADY_DONE: OUTCOME_ALREADY_DONE}[result.outcome]
    logger.info("adhoc webhook: outcome=%s deal=%s job=%s", outcome, deal_id, result.job.id)
    if outcome == OUTCOME_ALREADY_DONE:
        _blocked_notice(deal_id, result.job)
    return _data(outcome, result.job), 200


def _blocked_notice(deal_id: str, job: store.Job) -> None:
    """Tell people a deal that was already pushed to Famly was sent again and blocked. Never changes the reply."""
    try:
        from .messages import blocked_text
        from .notifier import SlackNotifier
        SlackNotifier().notice(blocked_text(deal_id, job.updated_at))
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc webhook: blocked notice hit %s", type(exc).__name__)
