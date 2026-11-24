"""adhoc-audit: READ-ONLY audit of the HubSpot "Ad Hoc" pipeline against Famly.

Phase 1 of docs/adhoc_design.md. For every Ad Hoc deal it reports, as codes only:

  * the booking data: `ad_hoc_date_1..10` + `ad_hoc_session_1..10` (Morning | Afternoon | Full Day),
    `ad_hoc_end_date`: missing or unknown slots, bad or duplicate or past or weekend dates, and an
    end date that is missing or earlier than the last session;
  * whether the child is already in Famly (EYWorks children are, with no plan), found the same way
    onboard-audit finds it: deal reference, recorded famly_childid, URN;
  * whether that child has any plan, is enrolled at the deal's site, and is enrolled on every
    session date.

Nothing is written anywhere. HubSpot goes through ReadOnlyHubSpot (GET and the deal search only), Famly
GraphQL through ReadOnlyGraphQL (no mutations), Famly REST through ReadOnlyRest (get only). Output is
counts and codes; the CSV holds deal ids, child ids and codes only -- never a name, email, date value or
amount. Child ids are fine.
"""

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import derive, links
from actions.onboard_dry_run.famly_reads import normalise_email
from core import hubspot as hubspot_cfg
from core.hubspot import HubSpotError

PIPELINE = "ad hoc"
MAX_SESSIONS = 10
SLOTS = {"morning", "afternoon", "full day"}
# Stages where past dates are expected (the booking has happened or is dead).
PAST_OK_STAGES = {"ad hoc started", "ad hoc completed", "closed lost"}

DATE_PROPERTIES = tuple(f"ad_hoc_date_{i}" for i in range(1, MAX_SESSIONS + 1))
SLOT_PROPERTIES = tuple(f"ad_hoc_session_{i}" for i in range(1, MAX_SESSIONS + 1))
ADHOC_PROPERTIES = DATE_PROPERTIES + SLOT_PROPERTIES + ("ad_hoc_end_date", "adhoc_discount")

SEARCH_BATCH = 100
# --contacts skips these (their sessions are in the past) unless include_past is set.
PAST_STAGES = {"ad hoc completed", "closed lost"}


def deal_properties() -> list[str]:
    return list(dict.fromkeys(list(hubspot_cfg.DEAL_PROPERTIES) + list(ADHOC_PROPERTIES)))


def parse_date(value) -> date | None:
    """A HubSpot date property: ISO text, or epoch milliseconds. None if empty or unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{12,13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def session_codes(deal: dict, stage: str | None, today: date) -> tuple[list[str], list[date]]:
    """(codes, valid session dates) from the deal's booking properties. Pure; codes only."""
    codes: list[str] = []
    dates: list[date] = []
    seen: set[date] = set()
    any_booked = False
    for date_prop, slot_prop in zip(DATE_PROPERTIES, SLOT_PROPERTIES):
        raw_date = str(deal.get(date_prop) or "").strip()
        raw_slot = str(deal.get(slot_prop) or "").strip()
        if not raw_date and not raw_slot:
            continue
        any_booked = True
        if raw_date and not raw_slot:
            codes.append("session_slot_missing")
        elif raw_slot and not raw_date:
            codes.append("session_date_missing")
        elif raw_slot.casefold() not in SLOTS:
            codes.append("session_slot_unknown")
        if not raw_date:
            continue
        parsed = parse_date(raw_date)
        if parsed is None:
            codes.append("session_date_invalid")
            continue
        if parsed in seen:
            codes.append("session_date_duplicate")
        seen.add(parsed)
        dates.append(parsed)
        if parsed < today and derive.norm_stage(stage) not in PAST_OK_STAGES:
            codes.append("session_date_past")
        if parsed.weekday() >= 5:
            codes.append("session_date_weekend")
    if not any_booked:
        codes.append("no_sessions")
    end_raw = str(deal.get("ad_hoc_end_date") or "").strip()
    end = parse_date(end_raw)
    if dates:
        if not end_raw:
            codes.append("end_date_missing")
        elif end is None:
            codes.append("end_date_invalid")
        elif end < max(dates):
            codes.append("end_date_before_last_session")
    return list(dict.fromkeys(codes)), dates


def enrolment_codes(child, site_id: str | None, dates: list[date]) -> list[str]:
    """Is the child enrolled at the deal's site, and on every session date? A blank last day is open."""
    if not site_id:
        return []
    mine = [(first, last) for site, first, last in child.relations if site == site_id]
    if not mine:
        return ["child_not_at_site"]
    codes = []
    for day in dates:
        text = day.isoformat()
        if not any((not first or first <= text) and (not last or text <= last) for first, last in mine):
            codes.append("session_outside_enrolment")
            break
    return codes


def contact_email_codes(deal_contacts, child, index) -> list[str]:
    """Do the deal's contacts' emails match a parent (Famly contact) on THIS child? Emails are held in
    memory for the comparison only and never printed or stored; the result is codes."""
    if not deal_contacts:
        return ["deal_no_contacts"]
    if not child.contacts:
        return ["child_has_no_parents"]
    with_email = 0
    matched = 0
    for contact in deal_contacts:
        found = audit.find_contact(child, contact)[0] if (contact.emails or contact.email) else None
        if contact.email or contact.emails:
            with_email += 1
        if found is not None:
            matched += 1
    if not with_email:
        return ["deal_no_contact_email"]
    if matched == 0:
        return ["contact_email_not_on_child"]
    return ["contact_email_on_child"] + (["some_contacts_not_on_child"] if matched < with_email else [])


def email_candidates(deal_contacts, index) -> list[str]:
    """Famly children (ids) that carry a parent whose email matches ANY deal contact's email."""
    ids: set[str] = set()
    for contact in deal_contacts or []:
        emails = (set(contact.emails) | {normalise_email(contact.email)}) - {""}
        for email in emails:
            ids |= index.contact_children.get(email, set())
    return sorted(ids)


@dataclass
class Row:
    deal_id: str
    site_code: str | None = None
    stage: str | None = None
    child_id: str | None = None
    found_via: str = audit.NOT_FOUND
    sessions: int = 0
    codes: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)     # child ids matched by parent email (ids only)


def adhoc_deals(hubspot, stages: dict, stage_names: list[str] | None = None) -> list[dict]:
    """Every deal in the Ad Hoc pipeline (optionally only these stage labels): one search per 100 ids."""
    wanted = {derive.norm_stage(s) for s in stage_names or []}
    ids = [sid for sid, (pipeline, stage) in stages.items()
           if derive.norm_stage(pipeline) == PIPELINE and (not wanted or derive.norm_stage(stage) in wanted)]
    if not ids:
        return []
    return hubspot.search_deals([{"propertyName": "dealstage", "operator": "IN", "values": ids}],
                                deal_properties())


def deals_by_id(hubspot, deal_ids: list[str]) -> list[dict]:
    """The named deals with the ad hoc properties (a search, so the extra properties come back)."""
    found: list[dict] = []
    for start in range(0, len(deal_ids), SEARCH_BATCH):
        chunk = deal_ids[start:start + SEARCH_BATCH]
        found += hubspot.search_deals(
            [{"propertyName": "hs_object_id", "operator": "IN", "values": chunk}], deal_properties())
    return found


def run(hubspot, gql, rest, *, deal_ids: list[str] | None = None, stage_names: list[str] | None = None,
        site_ids: list[str] | None = None, today: date | None = None, children=None,
        read_plans=None, contacts: bool = False, include_past: bool = False,
        read_contacts=None) -> tuple[dict, list[Row]]:
    """The audit. `deal_ids` None = every Ad Hoc deal. `children` / `read_plans` / `read_contacts` are
    for tests.

    `contacts` (--contacts): also compare each deal's contacts' emails with the parents on the Famly child
    (GETs only; emails stay in memory). Unless `include_past`, Closed Lost and Ad Hoc Completed deals are
    left out, as their sessions are in the past."""
    today = today or date.today()
    stages = hubspot.stage_names()
    if deal_ids is None:
        deals = adhoc_deals(hubspot, stages, stage_names)
    else:
        deals = deals_by_id(hubspot, deal_ids)
    if children is None:
        if site_ids is None:
            from actions.urn_lookup import runner as urn_runner
            site_ids = urn_runner._all_institution_ids()
        children = audit.fetch_children(gql, site_ids)
    index = audit.Index(children)
    if contacts and not include_past:
        deals = [d for d in deals
                 if derive.norm_stage(stages.get(str(d.get("dealstage")), ("", ""))[1]) not in PAST_STAGES]
    if contacts and read_contacts is None:
        labels = links.load_labels(hubspot)

        def read_contacts(deal_id):  # noqa: E306
            return links.deal_contacts(hubspot, labels, deal_id)
    if read_plans is None:
        from actions.read_child_plans import runner as plans_runner

        def read_plans(child_id):  # noqa: E306
            return list(plans_runner.run(child_id, client=rest).plans)

    rows: list[Row] = []
    for deal in deals:
        deal_id = str(deal["id"])
        row = Row(deal_id=deal_id)
        rows.append(row)
        pipeline, stage = stages.get(str(deal.get("dealstage")), ("", ""))
        row.stage = stage or None
        if deal_ids is not None and derive.norm_stage(pipeline) != PIPELINE:
            row.codes.append("not_ad_hoc_pipeline")
            continue
        site_id = None
        try:
            row.site_code, site_id = derive.site_for_location(deal.get("location_of_interest"))
        except derive.Hold:
            row.codes.append("site_unmapped")

        codes, dates = session_codes(deal, stage, today)
        row.sessions = len(dates)
        row.codes += codes

        deal_contacts = None
        if contacts:
            try:
                deal_contacts = read_contacts(deal_id)
            except HubSpotError:
                row.codes.append("contacts_not_readable")

        row.found_via, child_ids = index.find(deal, deal_id)
        if not child_ids:
            recorded = str(deal.get("famly_childid") or "").strip()
            row.codes.append("famly_childid_not_in_famly" if recorded else audit.CHILD_MISSING)
            if deal_contacts is not None:
                row.candidates = email_candidates(deal_contacts, index)
                row.codes.append({0: "email_no_match", 1: "email_matches_one_child"}.get(
                    len(row.candidates), "email_matches_several_children"))
            continue
        if len(child_ids) > 1:
            row.codes.append(audit.CHILD_AMBIGUOUS)
            continue
        row.child_id = child_ids[0]
        if deal_contacts is not None:
            row.codes += contact_email_codes(deal_contacts, index.children[row.child_id], index)
        row.codes += enrolment_codes(index.children[row.child_id], site_id, dates)
        try:
            row.codes.append("child_has_plan" if read_plans(row.child_id) else "child_no_plan")
        except Exception:  # noqa: BLE001 - one child's plan read must not stop the audit
            row.codes.append("plans_not_readable")
    return summarise(rows, hubspot, gql, rest), rows


def summarise(rows: list[Row], hubspot, gql, rest) -> dict:
    by_code = Counter(code for r in rows for code in r.codes)
    return {
        "mode": "read_only",
        "dealsRead": len(rows),
        "sessionsTotal": sum(r.sessions for r in rows),
        "childrenFoundBy": dict(sorted(Counter(r.found_via for r in rows).items())),
        "byCode": dict(sorted(by_code.items())),
        "byCodeByStage": {code: dict(sorted(Counter(r.stage or "unknown" for r in rows if code in r.codes).items()))
                          for code in sorted(by_code)},
        "bySite": dict(sorted(Counter(r.site_code or "unknown" for r in rows).items())),
        "byStage": dict(sorted(Counter(r.stage or "unknown" for r in rows).items())),
        "requests": {"hubspot": getattr(hubspot, "calls", None), "famlyGraphql": getattr(gql, "calls", None),
                     "famlyRest": getattr(rest, "calls", None)},
    }


def write_csv(path: str, rows: list[Row], only_codes=None) -> int:
    """dealId, siteCode, stage, childId, found_via, sessions, codes, emailMatchChildIds. Ids, counts and codes only."""
    wanted = {c.strip() for c in only_codes or [] if c and c.strip()}
    rows = [r for r in rows if not wanted or wanted & set(r.codes)]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["dealId", "siteCode", "stage", "childId", "found_via", "sessions", "codes",
                         "emailMatchChildIds"])
        for r in rows:
            writer.writerow([r.deal_id, r.site_code or "", r.stage or "", r.child_id or "", r.found_via,
                             r.sessions, ";".join(dict.fromkeys(r.codes)), ";".join(r.candidates)])
    return len(rows)
