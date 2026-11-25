"""Step 3b: create a NEW ad hoc child, its contacts and the bill payer in Famly. A slim port of the onboarding worker's
`_ensure_child`, `_create_contacts`, `_create_one` and recipient stage (actions/onboard/worker.py), sharing only the
pure helpers and the proven Famly writers. Every request is sent ONCE and never retried; an unclear outcome is settled
by READS. Names, emails and phones are read from HubSpot into memory for the call and never stored, logged or posted:
outcomes are codes and HubSpot / Famly ids only.

Rules (docs/adhoc_3b_design.md): the live lookup for an existing child comes first (found = stop, nothing is created);
`child_create_sent` is persisted BEFORE the create; contacts are tried independently, bill payer first; a contact,
bill payer or recipient problem never stops the booking (the caller books once the child is confirmed).
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from actions.onboard import bill_payer_writes, famly_writes, inputs
from actions.onboard_dry_run import bill_payers as payer_scan
from actions.onboard_dry_run.famly_reads import normalise_email

from . import store
from .child_input import build_adhoc_child_input

logger = logging.getLogger(__name__)

LOOKUP_RETRY_WAITS = (1, 2, 4, 8)
CREATED, PRESENT = "created", "present"
OK_OUTCOMES = (CREATED, PRESENT)
UNKNOWN_OUTCOME = "create_outcome_unknown"


class StopJob(Exception):
    """End the job in `status` with `code`; nothing further is written."""

    def __init__(self, status: str, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass
class ContactsResult:
    outcomes: list[str] = field(default_factory=list)          # contact:<hubspotId>:<code>
    contact_ids: dict = field(default_factory=dict)            # hubspot id -> Famly contact id (created or present)
    problems: list[str] = field(default_factory=list)          # codes, never names


def lookup_with_settle(sleep: Callable, fn: Callable[[], list], expect: str | None = None,
                       label: str = "lookup") -> tuple[list, bool]:
    """Read `fn()` (a lookup, never a mutation) until it shows what is expected. (ids of the last successful read,
    readable). An unreadable lookup is never "found 0". 2+ ids is not lag and returns at once."""
    attempts = len(LOOKUP_RETRY_WAITS) + 1
    ids: list = []
    readable = False
    for attempt in range(1, attempts + 1):
        try:
            ids, readable = list(fn()), True
        except Exception as exc:  # noqa: BLE001 - the text may carry a body: type only
            logger.warning("adhoc: lookup %s failed attempt=%d error=%s", label, attempt, type(exc).__name__)
            readable = False
        if readable and (len(ids) > 1 or ((expect in ids) if expect else bool(ids))):
            return ids, True
        if attempt < attempts:
            sleep(LOOKUP_RETRY_WAITS[attempt - 1])
    return (ids if readable else []), readable


def deal_matches_plan(deal: dict, plan: dict) -> bool:
    """The group re-derived from this deal's DOB at the planned first day equals the approved group id."""
    from datetime import date

    from actions.onboard_dry_run import derive
    try:
        birthday = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
        first = date.fromisoformat(plan["firstDay"])
        title = derive.group_title_for_age(derive.completed_months(birthday, first))
        return derive.group_id_for(plan["site"], title) == plan.get("groupId")
    except Exception:  # noqa: BLE001 - cannot re-derive: never create
        return False


def existing_children(famly, urn: str) -> list[str]:
    """Famly children whose externalId is the URN allocated for this deal (distinct ids): a live roster scan."""
    return list(dict.fromkeys(famly.children_by_urn(urn)))


def ensure_child(job, public, famly, plan: dict, deal: dict, sleep: Callable) -> str:
    """Create the child once and return its id (recorded on the job at once). Raises StopJob.

    The URN allocated for the deal is the key for every lookup here (the child carries no deal reference)."""
    deal_id, urn = job.deal_id, (plan.get("urn") or {}).get("urn")
    if not urn:
        raise StopJob(store.NEEDS_REVIEW, "urn_missing")      # nothing was sent; a child is never created without it
    try:
        found = existing_children(famly, urn)                # the live lookup immediately before the write
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc: pre-create lookup hit %s", type(exc).__name__)
        raise StopJob(store.NEEDS_REVIEW, "child_lookup_failed") from None
    if found:
        raise StopJob(store.NEEDS_REVIEW, "plan_changed")    # never write to a child the approval did not name
    try:
        from datetime import date
        payload = build_adhoc_child_input(deal, deal_id, plan["siteId"], plan.get("groupId"),
                                          date.fromisoformat(plan["firstDay"]), urn)
    except (inputs.InputIncomplete, KeyError, ValueError) as exc:
        raise StopJob(store.FAILED, getattr(exc, "code", "child_input_incomplete")) from None
    assert "lastDay" not in payload["sitesRelation"][0]
    assert all(ref["system"] != "HUBSPOT" for ref in payload["externalSystemsReferences"])
    if not store.mark_child_create_sent(job.id):             # persisted BEFORE the request: never a second create
        raise StopJob(store.NEEDS_REVIEW, "duplicate_create_blocked")
    try:
        child_id = famly_writes.create_child(public, payload)
    except (famly_writes.CreateRejected, famly_writes.OutcomeUnknown) as exc:
        rejected = exc.code if isinstance(exc, famly_writes.CreateRejected) else None
        landed, readable = lookup_with_settle(sleep, lambda: existing_children(famly, urn), label="settle")
        if len(landed) > 1:
            raise StopJob(store.NEEDS_REVIEW, "child_ambiguous") from None
        if not landed:
            if rejected and readable:
                raise StopJob(store.FAILED, rejected) from None
            raise StopJob(store.NEEDS_REVIEW, "child_outcome_unknown") from None
        child_id = landed[0]
    store.update(job.id, child_id=child_id)                  # at once: a crash from here never replays the create
    seen, readable = lookup_with_settle(sleep, lambda: existing_children(famly, urn), expect=child_id,
                                        label="readback")
    if not readable or seen != [child_id]:
        raise StopJob(store.NEEDS_REVIEW, "readback_child_lookup_failed")
    return child_id


def create_contacts(job, public, famly, plan: dict, contacts, child_id: str, sleep: Callable) -> ContactsResult:
    """Every contact is tried, independently, BILL PAYER FIRST. A failure is recorded and the next goes ahead."""
    result = ContactsResult()
    by_id = {str(c.hubspot_id): c for c in contacts}
    entries = sorted(plan["contacts"], key=lambda e: not e["isBillPayer"])
    creates_sent = 0
    for slot, entry in enumerate(entries):
        hid = str(entry["hubspotContactId"])
        code = "contact_error"
        try:
            contact = by_id[hid]
            target = normalise_email(contact.email)
            if creates_sent == 0:
                here: list = []                                # a child created a moment ago has no contacts
            else:
                here = famly.locate_contact(child_id, hid, contact.emails or target).ids
            if len(here) == 1:
                code = PRESENT
                result.contact_ids[hid] = here[0]
            elif len(here) > 1:
                code = "ambiguous_on_child"
            else:
                code, sent = _create_one(public, famly, contact, child_id, target, result.contact_ids, sleep)
                creates_sent += sent
        except Exception as exc:  # noqa: BLE001 - one contact must never stop the rest
            logger.error("adhoc: contact slot=%d hit %s", slot, type(exc).__name__)
            code = "contact_error"
        result.outcomes.append(f"contact:{hid}:{code}")
        if code not in OK_OUTCOMES:
            result.problems.append(f"contact:{hid}:{code}")
    return result


def _create_one(public, famly, contact, child_id, target, contact_ids: dict, sleep) -> tuple[str, int]:
    """(outcome code, create requests sent). A contact read back exactly once has its id recorded."""
    try:
        payload, _warnings = inputs.build_contact_input(contact, child_id)
    except inputs.InputIncomplete as exc:
        return exc.code, 0                                      # nothing was sent
    hid = str(contact.hubspot_id)
    try:
        famly_writes.create_contact(public, payload)
    except (famly_writes.CreateRejected, famly_writes.OutcomeUnknown) as exc:
        rows, readable = lookup_with_settle(sleep, lambda: famly.locate_contact(child_id, hid, target).ids,
                                            label="contact_settle")
        if len(rows) != 1:
            if not rows and readable and isinstance(exc, famly_writes.CreateRejected):
                return exc.code, 1
            return UNKNOWN_OUTCOME, 1
    rows, _readable = lookup_with_settle(sleep, lambda: famly.locate_contact(child_id, hid, target).ids,
                                         label="contact_readback")
    if len(rows) != 1:
        return "readback_failed", 1
    contact_ids[hid] = rows[0]
    return CREATED, 1


def bill_payer_stage(job, public, famly, plan: dict, contacts, child_id: str, sleep: Callable):
    """The onboarding bill payer stage with the decision approved at plan time. Never raises: an unexpected error
    after a request may have gone out is an UNKNOWN outcome."""
    site_id = plan["siteId"]
    try:
        return bill_payer_writes.run_stage(
            public=public, scan=lambda: famly.bill_payers(site_id), contacts=contacts, approved=plan["billPayer"],
            site_id=site_id, child_id=child_id, sleep=sleep, unresolved=False,
            lookup=lambda fn: lookup_with_settle(sleep, fn, label="bill_payer_readback"))
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc: bill payer stage hit %s", type(exc).__name__)
        return bill_payer_writes.StageResult(payer_scan.OUTCOME_UNKNOWN)


def recipient_stage(public, famly, plan: dict, contacts, contact_ids: dict, payer, sleep: Callable) -> tuple[str | None, bool]:
    """Link the bill payer contact as an invoice recipient when the approved plan says `will_add`. Returns (code or
    None when nothing was due, problem). Flags only; an unknown outcome is settled by reads, never re-sent."""
    section = plan.get("billPayer") or {}
    if (section.get("recipient") != "will_add" or payer is None or payer.code not in payer_scan.SUCCESS_CODES
            or not payer.bill_payer_id):
        return None, False
    contact = next((c for c in contacts if c.is_bill_payer), None)
    contact_id = contact_ids.get(str(contact.hubspot_id)) if contact else None
    if not contact_id:
        return bill_payer_writes.CONTACT_NOT_FOUND, True
    payer_id, site_id = payer.bill_payer_id, plan["siteId"]

    def read() -> list:
        payers = famly.bill_payers(site_id)
        match = next((p for p in payers or [] if p.id == payer_id), None)
        if match is None:
            raise RuntimeError("payer_not_listed")
        return sorted(match.recipients)

    try:
        linked = bill_payer_writes.link_recipient(
            public=public, contact_id=contact_id, payer_id=payer_id, read=read,
            lookup=lambda fn: lookup_with_settle(sleep, fn, expect=contact_id, label="recipient_readback"))
        code = linked.code
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc: recipient link hit %s", type(exc).__name__)
        code = bill_payer_writes.RECIPIENT_UNCONFIRMED
    return code, code not in (bill_payer_writes.RECIPIENT_LINKED, bill_payer_writes.RECIPIENT_PRESENT)
