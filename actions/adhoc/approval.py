"""The Slack clicks for an ad hoc job: Approve / Reject the plan, answer the child-match question, Retry a hold.

Same pattern as actions/onboard/approval.py: a signed Slack interaction (verified upstream in server.py) arrives with
the button's value `<job_id>:<plan hash prefix>:<nonce>`, and the decision is an ATOMIC state change, so a double
click, or a click on a message that has been superseded, does nothing the second time. Anyone in the channel may
click. A click never writes to Famly: it returns the job to `queued` with the decision recorded, and the WORKER
re-plans and acts only if the plan is unchanged. Messages carry no names (the match question is posted by the
worker, not here).
"""

import logging

from integrations import slack

from . import config, store

logger = logging.getLogger(__name__)

HASH_PREFIX = 16

MSG_APPROVED = "✅ Approved. The worker will re-check the plan and then act on it."
MSG_REJECTED = "🚫 Rejected. Nothing was written."
MSG_MATCH_YES = "✅ Noted: that is the child. The worker will plan again and post the booking for approval."
MSG_MATCH_NEW = "✅ Noted: create a new child. The worker will plan again and post the booking for approval."
MSG_HANDLED = "This request has already been handled."
MSG_STALE = "⚠️ This request is out of date (the plan changed). Use the newer message."
MSG_UNKNOWN = "⚠️ Unknown ad hoc job."
MSG_RETRY_QUEUED = "🔁 Retry queued. The worker will read the deal again."
MSG_RETRY_HANDLED = "This Retry has already been used. Nothing new was started."
MSG_RETRY_STALE = "⚠️ This Retry button is out of date. Use the newer message."
MSG_RETRY_BUSY = "A job for this deal is already queued or running. Nothing new was started."
MSG_RETRY_DONE = "This deal has already been done. Nothing was queued."
MSG_RETRY_OFF = "The ad hoc pipeline is switched off. Nothing was queued."

_RESULT_CODES = {
    MSG_APPROVED: "approved", MSG_REJECTED: "rejected", MSG_MATCH_YES: "match_confirmed", MSG_MATCH_NEW: "match_new",
    MSG_HANDLED: "already_handled", MSG_STALE: "stale", MSG_UNKNOWN: "unknown", MSG_RETRY_QUEUED: "retry_queued",
    MSG_RETRY_HANDLED: "retry_already_used", MSG_RETRY_STALE: "retry_stale", MSG_RETRY_BUSY: "retry_job_active",
    MSG_RETRY_DONE: "retry_already_done", MSG_RETRY_OFF: "retry_mode_off",
}


def button_value(job_id: str, plan_hash: str, nonce: str | None) -> str:
    return f"{job_id}:{plan_hash[:HASH_PREFIX]}:{nonce or ''}"


def _parse(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value or ":" not in value:
        return None, None, None
    job_id, _, rest = value.partition(":")
    prefix, _, nonce = rest.partition(":")
    return job_id or None, prefix or None, nonce or None


def handle_click(action_id: str, value: str | None, user: str | None) -> str:
    """Act on a verified click. Never raises; returns the text to show. One INFO line per click (ids and codes)."""
    try:
        text = _decide(action_id, value, user)
    except Exception as exc:  # noqa: BLE001 - a click must never raise into the web route
        logger.error("ADHOC CLICK failed: %s", type(exc).__name__)
        text = MSG_UNKNOWN
    logger.info("ADHOC CLICK action=%s job=%s result=%s", action_id, _parse(value)[0],
                _RESULT_CODES.get(text, "unknown"))
    return text


def _decide(action_id: str, value: str | None, user: str | None) -> str:
    job_id, prefix, nonce = _parse(value)
    job = store.get(job_id) if job_id else None
    if job is None:
        return MSG_UNKNOWN
    if action_id == slack.ACTION_ADHOC_RETRY:
        return _retry(job, nonce, user)

    # Everything else answers a request the job is currently waiting on.
    if job.status != store.AWAITING_APPROVAL:
        return MSG_HANDLED
    if not job.approval_nonce or nonce != job.approval_nonce:
        return MSG_STALE
    if action_id == slack.ACTION_ADHOC_REJECT:
        if store.transition(job.id, store.AWAITING_APPROVAL, store.HELD, last_error_code="approval_rejected",
                            approval_nonce=None):
            logger.info("ADHOC REJECTED job=%s deal=%s user=%s -- nothing written", job.id, job.deal_id, user)
            return MSG_REJECTED
        return MSG_HANDLED
    if not job.plan_hash or not prefix or not job.plan_hash.startswith(prefix):
        return MSG_STALE

    if action_id == slack.ACTION_ADHOC_APPROVE and job.question == "plan":
        if store.transition(job.id, store.AWAITING_APPROVAL, store.QUEUED, approved_hash=job.plan_hash,
                            approval_nonce=None):
            logger.info("ADHOC APPROVED job=%s deal=%s user=%s", job.id, job.deal_id, user)
            return MSG_APPROVED
        return MSG_HANDLED
    if action_id in (slack.ACTION_ADHOC_MATCH_YES, slack.ACTION_ADHOC_MATCH_NEW) and job.question == "match":
        yes = action_id == slack.ACTION_ADHOC_MATCH_YES
        if yes and not job.candidate_child_id:
            return MSG_STALE
        if store.transition(job.id, store.AWAITING_APPROVAL, store.QUEUED, match_decision="confirmed" if yes else "new",
                            approval_nonce=None):
            logger.info("ADHOC MATCH job=%s deal=%s decision=%s user=%s", job.id, job.deal_id,
                        "confirmed" if yes else "new", user)
            return MSG_MATCH_YES if yes else MSG_MATCH_NEW
        return MSG_HANDLED
    return MSG_STALE                      # a button that does not fit the question the job is asking


def _retry(job, nonce, user) -> str:
    """Retry on a hold notice: the same enqueue as `adhoc-enqueue` (mode checked, a fresh job that plans again).
    Only the CURRENT notice's nonce counts, and it is spent atomically, so a second press does nothing."""
    if job.status != store.HELD or not job.approval_nonce:
        return MSG_RETRY_HANDLED
    if nonce != job.approval_nonce:
        return MSG_RETRY_STALE
    mode = config.mode()
    if mode == config.MODE_OFF:
        return MSG_RETRY_OFF
    if not store.consume_hold_nonce(job.id, nonce):
        return MSG_RETRY_HANDLED
    result = store.enqueue(job.deal_id, mode, source=job.source)
    logger.info("ADHOC RETRY job=%s deal=%s result=%s new_job=%s user=%s", job.id, job.deal_id, result.outcome,
                result.job.id, user)
    return {store.CREATED: MSG_RETRY_QUEUED, store.ALREADY_QUEUED: MSG_RETRY_BUSY,
            store.ALREADY_DONE: MSG_RETRY_DONE}[result.outcome]
