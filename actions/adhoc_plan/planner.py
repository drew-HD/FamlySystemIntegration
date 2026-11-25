"""adhoc-dry-run: what the ad hoc pipeline WOULD do for a deal. READ-ONLY; nothing is written anywhere.

Phase 3 of docs/adhoc_design.md. For one HubSpot Ad Hoc deal it works out, as codes, dates and prices:

  match   the Famly child: deal reference, recorded famly_childid, URN, then a parent's email (accepted only
          if the child's name / date of birth do not contradict the deal), then name + date of birth.
          Nothing matched = a new child would be created (with NO end date). A parent email that matches only a
          child with a different name or date of birth is a HOLD, never a guess (it may be a new sibling).
  who     "ad hoc only" = a new child, or an existing child with no plan. Those get the Ad Hoc Only tag and
          have their end date removed. A child with a plan is a regular child: no tag, no end-date change.
  discount  adhoc_discount is a fraction (0.05 = 5%) of the ad hoc total; Famly takes a flat amount, so
          discountAmount = total x fraction, rounded half up to the penny.
  book    one session per dated slot: the plain Morning / Afternoon / Full Day session, priced for the child's
          pricing group (under 3 or 3 and over, by age ON that date), conflict-previewed for an existing child.

HOLDS stop a real run (anything that could misprice or mis-book; nothing is guessed). FLAGS are informational.
Names, emails and dates of birth are read into memory to match and are never printed or stored by this module;
the plan carries ids, codes, dates and prices only.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from actions.adhoc_audit import runner as adhoc
from actions.adhoc_probe import writer
from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import derive
from core import hubspot as hubspot_cfg

GO_STAGE = "approved"
SLOT_TITLES = {"morning": "Morning", "afternoon": "Afternoon", "full day": "Full Day"}
# Session-code checks that do not matter here: the ad hoc path never sends an end date.
UNUSED_CODES = {"end_date_missing", "end_date_invalid", "end_date_before_last_session"}
OWN_OK_KINDS = {"INPUT_PURCHASE"}          # the session being booked, as Famly's preview marks it


@dataclass
class Deps:
    """What the planner reads. Each is a read; tests pass fakes."""
    has_plan: Callable[[str], bool]                       # child id -> does it have any plan
    read_sessions: Callable[[str, date], dict]            # child id, valid-on date -> sessions lookup body
    preview: Callable[[str, list, date], dict]            # child id, purchases, date -> previewUpdate result
    price_child_for_site: Callable[[str], str | None]     # site code -> a child id to price a NEW child with


def _tokens(*parts) -> frozenset:
    return frozenset(" ".join(str(p or "") for p in parts).casefold().split())


def deal_child_tokens(deal: dict) -> frozenset:
    return _tokens(*(deal.get(p) for p in hubspot_cfg.CHILD_NAME_PROPERTIES))


def deal_dob(deal: dict) -> date | None:
    try:
        return derive.parse_hubspot_date(deal.get("child_date_of_birth"))
    except ValueError:
        return None


def agrees(child, tokens: frozenset, dob: date | None) -> bool:
    """A candidate child does not contradict what the deal says about the child's name and birth date."""
    checks = []
    if tokens:
        checks.append(audit._tokens(child.full_name) == tokens)
    if dob and child.birthday:
        checks.append(child.birthday == dob.isoformat())
    return all(checks) if checks else True


def match_child(deal: dict, deal_id: str, contacts, index) -> tuple[str, list[str], list[str]]:
    """(how, child ids, extra holds/flags). how: deal_reference | famly_childid | urn | parent_email |
    name_dob | none."""
    via, ids = index.find(deal, deal_id)
    if ids:
        return via, ids, []
    tokens, dob = deal_child_tokens(deal), deal_dob(deal)
    candidates = adhoc.email_candidates(contacts, index) if contacts else []
    kept = [c for c in candidates if agrees(index.children[c], tokens, dob)]
    if kept:
        flags = [] if (tokens or dob) else ["matched_by_email_only"]
        return "parent_email", kept, flags
    if candidates:
        return "none", [], ["HOLD:parent_email_matches_a_different_child"]
    if tokens and dob:
        named = [c.id for c in index.children.values()
                 if audit._tokens(c.full_name) == tokens and c.birthday == dob.isoformat()]
        if named:
            return "name_dob", named, []
    return "none", [], []


def deal_sessions(deal: dict) -> list[tuple[date, str]]:
    """(date, session title) for every complete, valid booking pair on the deal, in date order."""
    out = []
    for date_prop, slot_prop in zip(adhoc.DATE_PROPERTIES, adhoc.SLOT_PROPERTIES):
        day = adhoc.parse_date(deal.get(date_prop))
        title = SLOT_TITLES.get(str(deal.get(slot_prop) or "").strip().casefold())
        if day and title:
            out.append((day, title))
    return sorted(set(out))


def date_details(deal: dict, since: date | None = None) -> dict[str, list[str]]:
    """Which dates each session-data hold is about, as ISO dates (no names, no personal data)."""
    seen, dups, weekend = set(), set(), set()
    for prop in adhoc.DATE_PROPERTIES:
        day = adhoc.parse_date(deal.get(prop))
        if day is None or (since and day < since):
            continue
        if day in seen:
            dups.add(day)
        seen.add(day)
        if day.weekday() >= 5:
            weekend.add(day)
    return {code: sorted(d.isoformat() for d in days)
            for code, days in (("session_date_duplicate", dups), ("session_date_weekend", weekend)) if days}


def own_conflicts(result: dict, booked: set[tuple[str, str]]) -> list[dict]:
    """The conflicts Famly's preview reports for the sessions we are actually booking: [{"date", "kind"}], by date.

    `booked` is the set of (session id, ISO date) pairs in THIS booking. A preview entry counts only if it is for
    one of those pairs: Famly also lists, on each booked date, the OTHER sessions that booking blocks
    (`ANOTHER_INPUT_PURCHASE`), and a session that is merely booked on some other day must not match them. The
    session being booked is marked `INPUT_PURCHASE` (not a conflict). Any message is a conflict. An entry with no
    date is matched on the session alone (the cautious side)."""
    sessions = {sid for sid, _day in booked}
    found = [{"date": str(m.get("date") or "")[:10], "kind": str(m.get("conflictKind") or "conflict")}
             for m in result.get("newPurchaseConflictMessages") or []]
    for u in result.get("unbookableSessions") or []:
        day = str(u.get("date") or "")[:10]
        ours = (u.get("sessionId"), day) in booked if day else u.get("sessionId") in sessions
        if ours and u.get("conflictKind") not in OWN_OK_KINDS:
            found.append({"date": day, "kind": str(u.get("conflictKind") or "conflict")})
    return sorted(found, key=lambda c: (c["date"], c["kind"]))


def build_plan(deal: dict, stage: str | None, contacts, index, deps: Deps, today: date,
               confirmed_child_id: str | None = None, force_new: bool = False) -> dict:
    deal_id = str(deal["id"])
    plan: dict[str, Any] = {"dealId": deal_id, "stage": stage, "holds": [], "flags": [], "steps": [],
                            "match": {"via": "none", "childIds": []}, "adHocOnly": None, "bookings": []}
    holds, flags = plan["holds"], plan["flags"]
    if derive.norm_stage(stage) != GO_STAGE:
        flags.append("stage_not_approved")

    try:
        site, site_id = derive.site_for_location(deal.get("location_of_interest"))
        plan["site"] = site
        plan["siteId"] = site_id
    except derive.Hold:
        holds.append("site_unmapped")
        return plan

    # The pipeline never books a session in the past (whatever the stage). Today or later is future. Past dates
    # are skipped when at least one date is today or later; a deal with no such date is held.
    codes, dates = adhoc.session_codes(deal, None, today)
    skip_codes = {"session_date_past", "session_date_weekend", "session_date_duplicate"}
    holds += [c for c in codes if c not in UNUSED_CODES and c not in skip_codes]
    sessions = deal_sessions(deal)
    past = sorted({d for d in dates if d < today})
    mixed = bool(past) and any(d >= today for d in dates)
    # weekend / duplicate apply only to dates that are actually booked (skipped past dates are not judged)
    details = date_details(deal, today if mixed else None)
    holds += [c for c in ("session_date_duplicate", "session_date_weekend") if c in details]
    plan["details"] = {code: days for code, days in details.items() if code in holds}
    if mixed:
        plan["skippedPast"] = [d.isoformat() for d in past]
        flags.append("past_dates_skipped")
        sessions = [(d, t) for d, t in sessions if d >= today]
    elif past:
        holds.append("no_future_sessions")
    # adhoc_discount is stored as a FRACTION of the ad hoc total (0.05 = 5%, 0.325 = 32.5%; checked against the
    # live deals). Famly's discountAmount is a FLAT amount, so it is computed below from the sessions total.
    try:
        fraction = Decimal(str(deal.get("adhoc_discount") or "0"))
    except Exception:  # noqa: BLE001
        fraction = None
    if fraction is None or not (Decimal("0") <= fraction <= Decimal("1")):
        holds.append("discount_invalid")                # not a number, or outside 0..1: never guessed or capped
        fraction = Decimal("0")
    plan["discountFraction"] = str(fraction)

    # A person's answer to the Slack match question: "that is the child" or "create a new child". It never
    # overrides a match the deal itself carries (reference, recorded id, URN).
    if confirmed_child_id and confirmed_child_id in index.children:
        how, ids, extra = "slack_confirmed", [confirmed_child_id], []
    else:
        how, ids, extra = match_child(deal, deal_id, contacts, index)
        if force_new and not ids:
            extra = [e for e in extra if e != "HOLD:parent_email_matches_a_different_child"]
            flags.append("new_child_confirmed_in_slack")
    plan["match"] = {"via": how, "childIds": ids}
    if "HOLD:parent_email_matches_a_different_child" in extra:
        plan["match"]["candidateIds"] = adhoc.email_candidates(contacts, index)
    holds += [e.removeprefix("HOLD:") for e in extra if e.startswith("HOLD:")]
    flags += [e for e in extra if not e.startswith("HOLD:")]
    if len(ids) > 1:
        holds.append("child_ambiguous")
        return plan

    child = index.children[ids[0]] if ids else None
    birthday = None
    if child is not None:
        birthday = date.fromisoformat(child.birthday) if child.birthday else None
        try:
            plan["adHocOnly"] = not deps.has_plan(child.id)
        except Exception:  # noqa: BLE001
            holds.append("plans_not_readable")
    else:
        plan["adHocOnly"] = True
        birthday = deal_dob(deal)
    if birthday is None:
        holds.append("birthday_missing")

    if child is not None and sessions:
        mine = [(f, l) for s, f, l in child.relations if s == site_id]
        if not mine:
            holds.append("child_not_at_site")
        else:
            if any(f and sessions[0][0].isoformat() < f for f, _l in mine):
                holds.append("child_starts_after_first_session")
            # An enrolment that has already ENDED is no hold: Famly accepts a booking after the end date (proved on
            # a test child), so the session is simply added. A session BEFORE the enrolment starts is untested.

    # ----- sessions and prices ------------------------------------------------------------------------------
    lookup_child = child.id if child else deps.price_child_for_site(site)
    purchase = None
    if sessions and not holds_block_pricing(holds) and lookup_child and birthday:
        try:
            body = deps.read_sessions(lookup_child, sessions[0][0])
            bookings, session_ids, total = [], [], Decimal("0")
            for day, title in sessions:
                group = writer.pricing_group_for(site, birthday, day)
                one, session_id = writer.build_purchase(lookup_child, body, group, day, title)
                booking = one["sessionBookings"][0]
                bookings.append(booking)
                session_ids.append(session_id)
                total += Decimal(booking["bookedPrice"])
                plan["bookings"].append({"date": day.isoformat(), "session": title, "price": booking["bookedPrice"],
                                         "pricingGroup": group, "ageBand": writer.age_band(birthday, day)})
            discount_amount = (total * fraction).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            plan["purchase"] = None          # set below, once the purchase is built
            plan["total"] = f"{total:.2f}"
            plan["discountAmount"] = f"{discount_amount:.2f}"
            plan["totalAfterDiscount"] = f"{total - discount_amount:.2f}"
            purchase = {"childId": child.id if child else None, "discountAmount": f"{discount_amount:.2f}",
                        "productBookings": [],
                        "sessionBookings": bookings}
            plan["purchase"] = purchase     # EXACTLY what a live run sends: ids, prices and discount, fixed at approval
            if child is not None:
                result = deps.preview(child.id, [purchase], sessions[0][0]) or {}
                booked = {(sid, d.isoformat()) for sid, (d, _t) in zip(session_ids, sessions)}
                conflicts = own_conflicts(result, booked)
                if conflicts:
                    holds.append("conflict_preview_not_clean")
                    plan["details"]["conflict_preview_not_clean"] = conflicts
            else:
                flags.append("conflicts_not_checked_new_child")
        except writer.WriteRefused as exc:
            holds.append(str(exc).split(" ")[0])
    elif sessions and not lookup_child:
        holds.append("price_lookup_unavailable")

    # ----- the steps a real run would take --------------------------------------------------------------------
    if child is None:
        plan["steps"] += ["create_child_no_end_date", "create_contacts"]
    if plan["adHocOnly"]:
        plan["steps"].append("assign_ad_hoc_only_tag")
        if child is not None:
            plan["steps"].append("remove_end_date_if_set")
    if plan["bookings"]:
        plan["steps"].append(f"book_{len(plan['bookings'])}_sessions")
    return plan


def holds_block_pricing(holds: list[str]) -> bool:
    """Session-data problems that make pricing meaningless (the rest are reported alongside the prices)."""
    return any(h in holds for h in ("no_sessions", "session_date_invalid", "session_slot_missing",
                                    "session_date_missing", "session_slot_unknown", "site_unmapped",
                                    "no_future_sessions"))


def describe(plan: dict) -> list[str]:
    match = plan["match"]
    child = (f"existing {match['childIds'][0]} via {match['via']}" if len(match["childIds"]) == 1
             else "NEW child" if not match["childIds"] and not any("different_child" in h for h in plan["holds"])
             else f"{len(match['childIds'])} candidates ({match['via']})")
    lines = [f"deal {plan['dealId']} [{plan.get('stage')}] site={plan.get('site', '?')}: {child}; "
             f"ad hoc only={plan['adHocOnly']}"]
    for b in plan["bookings"]:
        lines.append(f"    {b['date']} {b['session']:<9} {b['price']:>7}  {b['ageBand']} group {b['pricingGroup'][:8]}")
    if plan.get("total"):
        lines.append(f"    total {plan['total']}"
                     + (f", discount {plan['discountFraction']} = -{plan['discountAmount']}, "
                        f"to pay {plan['totalAfterDiscount']}" if plan["discountAmount"] != "0.00" else ""))
    lines.append(f"    steps: {', '.join(plan['steps']) or 'none'}")
    lines.append(f"    HOLDS: {', '.join(plan['holds']) or 'none'}" + (f"   flags: {', '.join(plan['flags'])}"
                                                                      if plan["flags"] else ""))
    return lines
