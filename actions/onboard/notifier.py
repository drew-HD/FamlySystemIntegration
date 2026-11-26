"""Slack messages for the onboarding job. NO NAMES, emails, phones, addresses or
dates of birth: a deal id, a link, codes, counts and presence flags only.

`SlackNotifier` is the real one; the worker takes any object with the same two
methods, so tests pass a fake and scan what it was handed.
"""

import logging

from integrations import slack

from . import approval, config, messages, plan_status

logger = logging.getLogger(__name__)


def gate_text(
    plan: dict,
    plan_hash: str,
    mode: str,
    plan_changed: bool = False,
    this_job_child_id: str | None = None,
) -> str:
    """The Gate A message body for a plan. `this_job_child_id` is the child this
    very job created (if any), to tell `created by this job` from `created by
    pipeline` (a child an EARLIER job created)."""
    deal_id = plan["dealId"]
    link = config.deal_link(deal_id)
    child = plan.get("child") or {}
    dates = plan.get("dates") or {}

    child_only = plan.get("scope") == "child_only"
    lines = []
    writes = plan_status.planned_writes(plan)
    if child_only:
        lines.append("*[DRY RUN] Onboarding: would create the child in Famly (child only)*"
                     if mode == config.MODE_DRY
                     else "*Onboarding approval: create the child in Famly (child only)*")
    elif child.get("join"):
        lines.append("*[DRY RUN] Onboarding: would join the ad hoc child already in Famly*"
                     if mode == config.MODE_DRY
                     else "*Onboarding approval: join the ad hoc child already in Famly*")
    elif mode == config.MODE_DRY:
        lines.append("*[DRY RUN] Onboarding: would create the child and contacts in Famly*")
    else:
        lines.append("*Onboarding approval: create the child and contacts in Famly*")
    if plan_changed:
        lines.append("⚠️ The plan changed since it was last approved; approval is needed again.")
    lines.append(f"Deal: <{link}|{deal_id}>" if link else f"Deal: {deal_id} (set HUBSPOT_PORTAL_ID for a link)")
    if dates.get("lastDayDefaulted") and (child.get("status") == "child_would_create" or child.get("join")):
        end_text = "defaulted (31 August after the 4th birthday)"
    elif dates.get("lastDayDefaulted"):
        end_text = "none on the deal"          # an existing child: no end date is sent
    else:
        end_text = "present" if dates.get("lastDayPresent") else "none"
    lines.append(
        f"Site: {plan.get('siteCode')} · Room: {plan.get('groupTitle')} · "
        f"Start date: {'present' if dates.get('firstDayPresent') else 'missing'} · "
        f"End date: {end_text}"
    )
    urn = plan.get("urn") or {}
    if urn.get("status") == "allocated":
        lines.append(f"Child URN: a NEW URN will be assigned: {urn.get('urn')}. It is written to the "
                     "HubSpot deal first, then used for the Famly child (not counted as a Famly write).")
    elif urn.get("status") == "would_allocate":
        lines.append(f"Child URN: a new URN would be assigned (next number {urn.get('nextNumber'):04d} "
                     f"for site {urn.get('siteCode')}, not reserved yet).")
    child_writes = writes["child"]
    lines += plan_status.block(plan, this_job_child_id)
    if child_only:
        lines.append(f"Famly writes planned: {child_writes} child"
                 + (", the ad hoc child's dates and tag" if writes["join"] else ""))
        shown = messages.classify(plan.get("warnings") or [])
        sentences = shown[messages.NOT_DONE] + shown[messages.CHECK]
        lines.append("Warnings: " + (" ".join(sentences) if sentences else "none"))
        lines.append(f"Plan: `{plan_hash[:approval.HASH_PREFIX]}`")
        return "\n".join(lines)
    # The count equals the writes the worker will make (`plan_status.planned_writes`): the bill
    # payer create or attach is one write; a present or flagged payer is none.
    bp_section = plan.get("billPayer")
    lines.append(
        f"Famly writes planned: {child_writes} child, {writes['contact']} contact(s)"
        + (f", {writes['bill_payer']} bill payer" if isinstance(bp_section, dict) else "")
        + (f", {writes['relation']} contact relation(s)" if writes["relation"] else "")
        + (f", {writes['recipient']} bill payer contact link(s)" if writes["recipient"] else "")
        + (", the ad hoc child's dates and tag" if writes["join"] else "")
    )
    shown = messages.classify(plan.get("warnings") or [])
    sentences = shown[messages.NOT_DONE] + shown[messages.CHECK]
    lines.append("Warnings: " + (" ".join(sentences) if sentences else "none"))
    lines.append(f"Plan: `{plan_hash[:approval.HASH_PREFIX]}`")
    return "\n".join(lines)


def deal_ref(deal_id: str) -> str:
    link = config.deal_link(deal_id)
    return f"<{link}|{deal_id}>" if link else str(deal_id)


def notice_text(deal_id: str, headline: str, codes, extra_footer=()) -> str:
    """The staff notice: a headline, plain-English "Not done" and "To check" lists,
    and a small code footer. Configuration (admin) codes are NOT in it: see
    `admin_notice_text`."""
    return messages.staff_text(deal_ref(deal_id), headline, codes, extra_footer)


def same_child_text(deal_id: str, child_id: str, child_name: str | None, mode: str) -> str:
    """The registry same-child question. Carries the registry child's NAME, Famly id and deal id (the Slack
    exception for this question); never a date of birth, email or phone number."""
    dry = "[DRY RUN] " if mode == config.MODE_DRY else ""
    return "\n".join([
        f"*{dry}Onboarding: is this the same child?*",
        f"Deal: {deal_ref(deal_id)}",
        "A child already in Famly has the same date of birth and a similar name (it may be the same child, or a "
        "twin or sibling). Nothing has been created.",
        f"Famly child: *{child_name or 'name not available'}* (id {child_id})",
        "Check the deal and the child. If it is the same child press *That's the child* (the deal will join that "
        "child after the usual approval); if not, press *Different child* (a new child will be created after the "
        "usual approval).",
    ])


JOIN_WORDS = {
    "join_start_moved": "Start date: moved earlier to the nursery start.",
    "join_start_kept": "Start date: kept (the child already started on or before the nursery start).",
    "join_end_set": "End date: nursery end date set.",
    "join_end_kept": "End date: already the nursery end date.",
    "join_end_date_failed": "End date: NOT set. Set it in Famly by hand.",
    "join_dates_failed": "Dates: NOT confirmed. Check the start and end dates in Famly.",
    "join_summary_unreadable": "Dates and tag: NOT changed (the child could not be read). Do them in Famly by hand.",
    "join_tag_removed": "Ad Hoc Only tag: removed.",
    "join_tag_absent": "Ad Hoc Only tag: was not on the child.",
    "join_tag_not_found": "Ad Hoc Only tag: could not be found for the child's nursery. Check the child's tags.",
    "join_tag_failed": "Ad Hoc Only tag: NOT removed. Remove it in Famly by hand.",
    "join_urn_written": "URN: the ad hoc child's URN was saved on the deal.",
    "join_urn_write_failed": "URN: could NOT be saved on the deal. Copy it by hand.",
    "join_urn_differs": "URN: the deal already has a different URN. Check which is right.",
}


def join_summary_text(deal_id: str, child_id: str, child_name: str | None, codes) -> str:
    """The join summary (operations joining an ad hoc child): the child's NAME, Famly id and deal id (the Slack
    exception for join summaries), and what the join did. The room and booked sessions were not changed."""
    lines = [f"🔗 Nursery deal {deal_ref(deal_id)} joined the ad hoc child *{child_name or 'name not available'}* "
             f"(Famly id {child_id}). The room and the booked ad hoc sessions were not changed."]
    lines += [JOIN_WORDS[c] for c in codes if c in JOIN_WORDS]
    return "\n".join(lines)


def admin_notice_text(deal_id: str, codes) -> str | None:
    """The one separate admin line for configuration problems, or None."""
    return messages.admin_text(deal_ref(deal_id), codes)


class SlackNotifier:
    """Posts through integrations.slack. Never raises."""

    def gate(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        """Post the Approve / Reject request; returns the message ref or None."""
        return slack.post_onboard_gate(text, approval.button_value(job_id, plan_hash, nonce))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)

    def same_child(self, text: str, job_id: str, child_id: str, nonce: str) -> str | None:
        """The registry same-child question; returns the message ref or None."""
        return slack.post_onboard_same_child(text, approval.same_child_value(job_id, child_id, nonce))

    def hold_notice(self, text: str, job_id: str, nonce: str) -> bool:
        """A hold notice with a Retry button."""
        return slack.post_onboard_hold(text, approval.button_value(job_id, "", nonce)) is not None
