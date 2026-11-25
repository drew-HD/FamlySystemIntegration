"""Slack text for the ad hoc pipeline. Deal ids, Famly child ids, dates, session titles, prices and codes only,
EXCEPT the child-match question, which also carries the Famly child's name (Drew, 2026-11-24: otherwise nobody can
check the match). That name is passed in at post time from memory and is never stored.
"""

import os
from datetime import date
from decimal import Decimal

DRY = "[DRY RUN] "

SITE_NAMES = {"HDWEST": "Westminster", "HDCW": "Canada Water", "HDCITY": "City"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def site_name(code) -> str:
    return SITE_NAMES.get(str(code), str(code or "unknown site"))


def long_date(iso: str) -> str:
    """2026-11-20 -> '20 Nov 2026' (fixed English month names, whatever the server locale)."""
    day = date.fromisoformat(str(iso)[:10])
    return f"{day.day:02d} {_MONTHS[day.month - 1]} {day.year}"


def percent(fraction) -> str:
    """The deal's fraction as a percent: 0.05 -> '5%', 0.325 -> '32.5%'."""
    return f"{(Decimal(str(fraction)) * 100).normalize():f}%"

# Plain-English sentences for the hold codes a person can act on.
SENTENCES = {
    "worker_error_after_write": "The worker hit an error after the booking was sent, so it may or may not be complete. "
                                "Check the child's bookings in Famly. The booking was NOT sent again.",
    "stage_not_eligible": "The deal is not at Approved in the Ad Hoc pipeline.",
    "not_ad_hoc_pipeline": "The deal is not in the Ad Hoc pipeline.",
    "site_unmapped": "The deal's location is not Westminster, City or Canada Water.",
    "no_sessions": "The deal has no booked dates.",
    "session_slot_missing": "A booked date has no Morning / Afternoon / Full Day.",
    "session_date_missing": "A session slot has no date.",
    "session_slot_unknown": "A session slot is not Morning, Afternoon or Full Day.",
    "session_date_invalid": "A booked date could not be read.",
    "session_date_duplicate": "The same date is booked twice.",
    "no_future_sessions": "Every booked date is in the past, so there is nothing to book.",
    "session_date_past": "A booked date is in the past.",
    "session_date_weekend": "A booked date is a weekend.",
    "discount_invalid": "The deal's discount is not a number between 0 and 1.",
    "birthday_missing": "The child's date of birth is missing.",
    "child_ambiguous": "More than one Famly child matches this deal.",
    "parent_email_matches_a_different_child": "A parent's email matches a Famly child with a different name or date of birth.",
    "child_not_at_site": "The child is not enrolled at this site in Famly.",
    "child_starts_after_first_session": "The child's Famly enrolment starts after the first booked date.",
    "plans_not_readable": "The child's Famly plans could not be read.",
    "conflict_preview_not_clean": "Famly reports a booking conflict for these dates.",
    "price_lookup_unavailable": "No price could be looked up for this site.",
    "pricing_group_unmapped": "No pricing group is set up for this site and age.",
    "price_missing_for_pricing_group": "Famly has no price for this session and pricing group.",
    "session_not_found": "Famly has no matching Morning / Afternoon / Full Day session.",
    "availability_missing": "The session is not offered on one of the booked weekdays.",
    "price_not_a_number": "A session price could not be read.",
    "price_not_positive": "A session price is zero or negative.",
    "deal_not_found": "The deal could not be read from HubSpot.",
    "group_missing": "No Famly room (group) is set up for this child's age at the first booked date.",
    "contacts_not_readable": "The deal's contacts could not be read from HubSpot.",
    "bill_payer_missing": "The deal needs exactly one contact marked as the Bill Payer.",
    "contact_email_missing": "A contact on the deal has no email address.",
    "contact_name_incomplete": "A contact on the deal has no first or last name.",
    "dates_inconsistent": "The child's date of birth is after the first booked date.",
    "urn_generation_off": "Child URN generation is switched off (ONBOARD_URN_GENERATION), so a new child cannot get a URN.",
    "urn_site_unknown": "There is no URN site code for this site.",
    "urn_not_available": "A URN could not be allocated (a read failed or the sequence is exhausted).",
    "plan_changed": "The Famly child (or the deal's contacts) changed after approval, so nothing was written. "
                    "Check the deal and Famly, then try again.",
    "child_lookup_failed": "Famly could not be read just before creating the child, so nothing was created.",
    "child_outcome_unknown": "The child create did not get a clear answer and no child was found. Check Famly. "
                             "It was NOT sent again.",
    "child_ambiguous": "More than one Famly child carries this deal's reference. Check Famly.",
    "urn_hubspot_write_failed": "The child's URN could not be written to the HubSpot deal, so nothing was created in Famly.",
    "hubspot_childid_write_failed": "The Famly child id could not be written to the HubSpot deal (the child and booking are fine).",
    "urn_missing": "No URN was allocated for this deal, so no child was created.",
    "readback_child_lookup_failed": "The child was created but could not be found again by its URN. "
                                    "Check Famly. Nothing else was written.",
    "duplicate_create_blocked": "This job had already sent its child create, so it was not sent again.",
    "deal_not_readable": "The deal or its contacts could not be read, so nothing was written.",
    "contacts_missing": "The child was created by an earlier attempt and its parent contacts may be missing. "
                        "Check the child's contacts in Famly.",
    "contact": "A parent contact was not created cleanly (HubSpot contact id and code shown). Check Famly.",
    "booking_rejected": "Famly refused the booking. Nothing was booked.",
    "purchase_not_for_this_child": "The booking did not match the child. Nothing was booked.",
    "booking_outcome_unknown": "The booking request did not get a clear answer, so it may or may not have gone in. "
                               "Check the child's bookings in Famly. It was NOT sent again.",
    "booking_mismatch": "The booking went in but Famly's figures differ from what was approved. Check it in Famly.",
    "duplicate_write_blocked": "This job had already sent its booking, so it was not sent again.",
    "interrupted": "The worker stopped part-way through writing. Check the child in Famly before doing anything else.",
    "tag_not_found": "The Ad Hoc Only tag was not found (or more than one) for this site, so it was not added.",
    "tag_failed": "The Ad Hoc Only tag could not be added.",
    "end_date_failed": "The child's end date could not be removed.",
    "approval_rejected": "The plan was rejected.",
    "registry_unreadable": "The children registry could not be read, so the job is held: no child is created or "
                           "matched until the registry can be checked. Press Retry once it is back.",
    "registry_similar_child": "A child in the children registry has a similar name and the same date of birth.",
    "slack_post_failed": "The approval message could not be posted to Slack.",
}


def deal_link(deal_id: str) -> str:
    portal = os.environ.get("HUBSPOT_PORTAL_ID", "").strip()
    return f"<https://app.hubspot.com/contacts/{portal}/record/0-3/{deal_id}|{deal_id}>" if portal else str(deal_id)


def _money(value) -> str:
    return f"£{value}"


def _deal_links(ids) -> str:
    return ", ".join(deal_link(i) for i in ids)


def operations_lines(plan: dict, child_name: str | None, booked: bool) -> list[str]:
    """Stage 3: the child already has an operations (nursery) deal. Carries the child's name and the deal ids
    (Drew, 2026-11-25: the join summary may name the child)."""
    ops = plan["match"].get("operationsDealIds") or []
    if not ops:
        return []
    child_id = plan["match"]["childIds"][0]
    lines = [f"This child has an operations deal (HubSpot {_deal_links(ops)}): the sessions "
             f"{'were' if booked else 'will be'} booked on the existing child, with no Ad Hoc Only tag and the end "
             "date kept. Please ask the parents to book on Famly next time."]
    if "operations_child_no_famly_plan" in plan["flags"]:
        lines.append(f"⚠️ Needs checking: {child_name or 'name not available'} (Famly Id: {child_id}) is marked as "
                     f"having an operations deal ({_deal_links(ops)}) but has no plan in Famly.")
    return lines


REGISTRY_WORDS = {
    "added": "Children registry: updated.",
    "already": "Children registry: already up to date.",
    "conflict": "⚠️ Needs checking: in the children registry this deal (or the URN) is linked to a different child, "
                "so it was not changed.",
    "failed": "⚠️ Needs checking: the children registry could not be updated.",
    "missing": "⚠️ Needs checking: this child is not in the children registry and could not be added (run "
               "registry-backfill).",
}


REOPEN_WARNING = ("⚠️ Manual re-run: this deal was already pushed to Famly. Check the child's bookings in Famly "
                  "before approving.")


def blocked_text(deal_id: str, done_at: str | None) -> str:
    """A done deal was sent again by the webhook and blocked (ids only)."""
    when = f" (job done on {long_date(done_at)})" if done_at else ""
    return (f"⚠️ Ad hoc deal {deal_link(deal_id)} was sent again, but its sessions were already pushed to Famly"
            f"{when}, so it was blocked. To re-run it: `python main.py adhoc-enqueue --deal-id {deal_id} "
            "--reopen-done`")


def gate_text(plan: dict, digest: str, mode: str, changed: bool = False, child_name: str | None = None,
              reopen: bool = False) -> str:
    """The plan approval message. For an EXISTING child it carries the Famly child's name and id (Drew,
    2026-11-24); `child_name` comes from memory at post time and is never stored."""
    dry = DRY if mode == "dry" else ""
    n = len(plan["bookings"])
    lines = [f"*{dry}Ad hoc approval: book {n} session{'s' if n != 1 else ''}*"]
    if reopen:
        lines.append(REOPEN_WARNING)
    if changed:
        lines.append("⚠️ The plan changed since it was last approved; approval is needed again.")
    lines.append(f"Deal: {deal_link(plan['dealId'])} · Site: {site_name(plan.get('site'))}")
    match = plan["match"]
    if len(match["childIds"]) == 1:
        lines.append(f"Child exists in Famly: {child_name or 'name not available'} (Famly Id: {match['childIds'][0]})")
        if plan.get("resumeContacts"):
            lines.append(f"Parent contacts: {len(plan.get('contacts') or [])} to be created for this child (an earlier "
                         "run made the child but stopped before its contacts)")
    else:
        lines.append("Child doesn't exist in Famly, to be created")
        lines += _new_child_lines(plan)
    lines.append("Ad hoc only child" if plan["adHocOnly"] else "Standard child booking ad hoc")
    lines += operations_lines(plan, child_name, booked=False)
    for number, b in enumerate(plan["bookings"], 1):
        lines.append(f"{number}. {long_date(b['date'])} {b['session']} {_money(b['price'])}")
    total = f"Total {_money(plan['total'])}"
    if plan["discountAmount"] != "0.00":
        total += (f" · Discount {percent(plan['discountFraction'])} (-{_money(plan['discountAmount'])})"
                  f" · To pay {_money(plan['totalAfterDiscount'])}")
    lines.append(total)
    if "conflicts_not_checked_new_child" in plan["flags"]:
        lines.append("Conflicts: not checked (the child does not exist yet).")
    if plan.get("skippedPast"):
        lines.append("Past dates not booked: " + ", ".join(long_date(d) for d in plan["skippedPast"]))
    lines.append(f"Steps: {', '.join(plan['steps'])}")
    lines.append(f"Plan: `{digest[:16]}`")
    return "\n".join(lines)


PAYER_WORDS = {"would_create": "a new bill payer will be created", "would_attach": "the child will be linked to the "
               "existing bill payer", "present": "the child is already linked to the bill payer",
               "flagged": "needs a person (the bill payer step will not write)"}


def _new_child_lines(plan: dict) -> list[str]:
    """What the new child will be created with. Counts and codes only: NO contact names."""
    if "firstDay" not in plan:
        return []
    contacts = plan.get("contacts") or []
    lines = [f"Group: {plan.get('groupTitle') or 'unknown'} · Start date: {long_date(plan['firstDay'])} · no end date",
             f"Contacts to create: {len(contacts)} (bill payer first, {sum(1 for c in contacts if c['isBillPayer'])} marked)"]
    if plan.get("billPayer"):
        decision = plan["billPayer"]
        lines.append("Bill payer: " + PAYER_WORDS.get(decision.get("status"), str(decision.get("status")))
                     + (f" ({decision['flag']})" if decision.get("flag") else ""))
    urn = plan.get("urn") or {}
    lines.append(f"URN: {urn['urn']}" if urn.get("urn") else "URN: will be assigned when approved")
    return lines


def match_text(plan: dict, famly_name: str, child_id: str, mode: str) -> str:
    """The child-match question. Carries the Famly child's NAME and the deal id (the one Slack exception)."""
    dry = DRY if mode == "dry" else ""
    why = ("A child in the children registry has the same date of birth and a similar name (it may be the same "
           "child, or a twin or sibling)." if plan["match"].get("reason") == "registry_similar" else
           "A parent's email on this deal matches a child in Famly, but the name or date of birth on the deal is "
           "different (it may be a sibling).")
    return "\n".join([
        f"*{dry}Ad hoc: is this the correct child?*",
        f"Deal: {deal_link(plan['dealId'])} · Site: {site_name(plan.get('site'))}",
        why,
        f"Famly child: *{famly_name or 'name not available'}* (id {child_id})",
        "Check the deal in HubSpot. If this is the right child press *That's the child*; if the deal is for a "
        "different child press *Create a new child*."])


CONFLICT_WORDS = {
    "ANOTHER_INPUT_PURCHASE": "another session on this booking is on the same day",
    "INPUT_PURCHASE": "this booking",
}


def _conflict_word(kind: str) -> str:
    return CONFLICT_WORDS.get(kind, kind.lower().replace("_", " "))


def _detail(code: str, details) -> str:
    """The dates a hold is about: ' (28 Dec 2026, 04 Jan 2027)'. A conflict also says what kind, per date:
    ': 28 Dec 2026 (another session on this booking is on the same day)'."""
    items = (details or {}).get(code) or []
    if not items:
        return ""
    if code == "conflict_preview_not_clean":
        return ": " + "; ".join(f"{long_date(c['date']) if c.get('date') else 'a date'} ({_conflict_word(c['kind'])})"
                                for c in items)
    return f" ({', '.join(long_date(d) for d in items)})"


def hold_text(deal_id: str, codes, mode: str, details=None) -> str:
    dry = DRY if mode == "dry" else ""
    sentences = [SENTENCES.get(c, f"({c})").rstrip(".") + _detail(c, details) + "." for c in codes]
    return "\n".join([f"*{dry}Ad hoc: not done, a person needs to check*", f"Deal: {deal_link(deal_id)}",
                      *[f"• {t}" for t in sentences],
                      "Fix it in HubSpot or Famly, then press Retry.", f"Codes: {', '.join(codes)}"])


def done_text(plan: dict, booked_ids: list, outcomes: dict, problems: list, mode: str,
              child_id: str | None = None, report: list | None = None) -> str:
    """The after-writing notice: counts, money and codes. `report` holds the registry stage lines (operations child,
    registry and HubSpot follow-up), which may name the child (Drew, 2026-11-25)."""
    n = len(plan["bookings"])
    head = "✅ Ad hoc done" if not problems else "⚠️ Ad hoc done with problems"
    lines = [f"{head} for deal {deal_link(plan['dealId'])}: {n} session{'s' if n != 1 else ''} booked "
             f"(total {_money(plan['total'])}"
             + (f", discount -{_money(plan['discountAmount'])}, to pay {_money(plan['totalAfterDiscount'])}"
                if plan["discountAmount"] != "0.00" else "") + ")."]
    if child_id:
        lines.append(f"New child created in Famly (Famly Id: {child_id}), with its contacts and bill payer.")
    if "tag" in outcomes:
        lines.append(f"Ad Hoc Only tag: {outcomes['tag'].replace('tag_', '').replace('_', ' ')}. "
                     f"End date: {outcomes['end'].replace('end_date_', '').replace('_', ' ')}.")
    lines += report or []
    if problems:
        lines += [f"• {SENTENCES.get(c.split(':')[0], c)}" for c in problems]
        lines.append(f"Codes: {', '.join(problems)}")
    return "\n".join(lines)
