"""Slack clicks on a deposit approval: Approve / Reject. Value `<job_id>:<nonce>`.

A click never writes to Famly: Approve moves the job awaiting_approval -> approved (atomic, nonce checked, so a
double click or an old message does nothing) and the worker writes on its next run, re-checking everything.
"""

import logging

from integrations import slack

from . import store

logger = logging.getLogger(__name__)

MSG_APPROVED = "✅ Approved. The deposit will be added and invoiced within a few minutes."
MSG_REJECTED = "🚫 Rejected. Nothing was written."
MSG_HANDLED = "This request has already been handled."
MSG_STALE = "⚠️ This request is out of date. Use the newer message."
MSG_UNKNOWN = "⚠️ Unknown deposit job."


def button_value(job_id: str, nonce: str) -> str:
    return f"{job_id}:{nonce}"


def handle_click(action_id: str, value: str | None, user: str | None) -> str:
    try:
        text = _decide(action_id, value)
    except Exception as exc:  # noqa: BLE001 - a click must never raise into the web route
        logger.error("DEPOSIT CLICK failed: %s", type(exc).__name__)
        text = MSG_UNKNOWN
    logger.info("DEPOSIT CLICK action=%s job=%s result=%s", action_id, (value or "").partition(":")[0],
                {MSG_APPROVED: "approved", MSG_REJECTED: "rejected", MSG_HANDLED: "handled",
                 MSG_STALE: "stale"}.get(text, "unknown"))
    return text


def _decide(action_id: str, value: str | None) -> str:
    job_id, _, nonce = (value or "").partition(":")
    job = store.get(job_id) if job_id else None
    if job is None:
        return MSG_UNKNOWN
    if job.status != store.AWAITING_APPROVAL:
        return MSG_HANDLED
    if not job.approval_nonce or nonce != job.approval_nonce:
        return MSG_STALE
    if action_id == slack.ACTION_DEPOSIT_APPROVE:
        ok = store.transition(job.id, store.AWAITING_APPROVAL, store.APPROVED, approval_nonce=None)
        return MSG_APPROVED if ok else MSG_HANDLED
    if action_id == slack.ACTION_DEPOSIT_REJECT:
        ok = store.transition(job.id, store.AWAITING_APPROVAL, store.REJECTED, approval_nonce=None)
        return MSG_REJECTED if ok else MSG_HANDLED
    return MSG_UNKNOWN
