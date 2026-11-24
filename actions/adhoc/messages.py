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
    "stage_not_eligible": "The deal is not at Approved in the Ad Hoc pipeline.",
    "not_ad_hoc_pipeline": "The deal is not in the Ad Hoc pipeline.",
    "site_unmapped": "The deal's location is not Westminster, City or Canada Water.",
    "no_sessions": "The deal has no booked dates.",
    "session_slot_missing": "A booked date has no Morning / Afternoon / Full Day.",
    "session_date_missing": "A session slot has no date.",
    "session_slot_unknown": "A session slot is not Morning, Afternoon or Full Day.",
    "session_date_invalid": "A booked date could not be read.",
    "session_date_duplicate": "The same date is booked twice.",
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
    "approval_rejected": "The plan was rejected.",
    "slack_post_failed": "The approval message could not be posted to Slack.",
}


def deal_link(deal_id: str) -> str:
    portal = os.environ.get("HUBSPOT_PORTAL_ID", "").strip()
    return f"<https://app.hubspot.com/contacts/{portal}/record/0-3/{deal_id}|{deal_id}>" if portal else str(deal_id)


def _money(value) -> str:
    return f"£{value}"


def gate_text(plan: dict, digest: str, mode: str, changed: bool = False, child_name: str | None = None) -> str:
    """The plan approval message. For an EXISTING child it carries the Famly child's name and id (Drew,
    2026-11-24); `child_name` comes from memory at post time and is never stored."""
    dry = DRY if mode == "dry" else ""
    n = len(plan["bookings"])
    lines = [f"*{dry}Ad hoc approval: book {n} session{'s' if n != 1 else ''}*"]
    if changed:
        lines.append("⚠️ The plan changed since it was last approved; approval is needed again.")
    lines.append(f"Deal: {deal_link(plan['dealId'])} · Site: {site_name(plan.get('site'))}")
    match = plan["match"]
    if len(match["childIds"]) == 1:
        lines.append(f"Child exists in Famly: {child_name or 'name not available'} (Famly Id: {match['childIds'][0]})")
    else:
        lines.append("Child doesn't exist in Famly, to be created")
    lines.append("Ad hoc only child" if plan["adHocOnly"] else "Standard child booking ad hoc")
    for number, b in enumerate(plan["bookings"], 1):
        lines.append(f"{number}. {long_date(b['date'])} {b['session']} {_money(b['price'])}")
    total = f"Total {_money(plan['total'])}"
    if plan["discountAmount"] != "0.00":
        total += (f" · Discount {percent(plan['discountFraction'])} (-{_money(plan['discountAmount'])})"
                  f" · To pay {_money(plan['totalAfterDiscount'])}")
    lines.append(total)
    if "conflicts_not_checked_new_child" in plan["flags"]:
        lines.append("Conflicts: not checked (the child does not exist yet).")
    lines.append(f"Steps: {', '.join(plan['steps'])}")
    lines.append(f"Plan: `{digest[:16]}`")
    return "\n".join(lines)


def match_text(plan: dict, famly_name: str, child_id: str, mode: str) -> str:
    """The child-match question. Carries the Famly child's NAME and the deal id (the one Slack exception)."""
    dry = DRY if mode == "dry" else ""
    return "\n".join([
        f"*{dry}Ad hoc: is this the correct child?*",
        f"Deal: {deal_link(plan['dealId'])} · Site: {site_name(plan.get('site'))}",
        "A parent's email on this deal matches a child in Famly, but the name or date of birth on the deal is "
        "different (it may be a sibling).",
        f"Famly child: *{famly_name or 'name not available'}* (id {child_id})",
        "Check the deal in HubSpot. If this is the right child press *That's the child*; if the deal is for a "
        "different child press *Create a new child*."])


def hold_text(deal_id: str, codes, mode: str) -> str:
    dry = DRY if mode == "dry" else ""
    sentences = [SENTENCES.get(c, f"({c})") for c in codes]
    return "\n".join([f"*{dry}Ad hoc: not done, a person needs to check*", f"Deal: {deal_link(deal_id)}",
                      *[f"• {t}" for t in sentences],
                      "Fix it in HubSpot or Famly, then press Retry.", f"Codes: {', '.join(codes)}"])
