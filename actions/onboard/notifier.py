"""Slack messages for the onboarding job.

Gate A (the approval request) carries the child's and parents' NAMES, the parents' EMAILS, the start and end
dates and the URN (Slack allows them, see CLAUDE.md). They are read from HubSpot into memory for the message
only (`gate_details`) and never go into the plan, its hash, the jobs table or a log. Phone numbers, addresses
and dates of birth never go in Slack. The other notices carry a deal id, a link, codes and counts, and a child
name where the Slack exception names it.

`SlackNotifier` is the real one; the worker takes any object with the same
methods, so tests pass a fake and scan what it was handed.
"""

import logging

from actions.adhoc.messages import site_name
from actions.onboard_dry_run import derive
from actions.onboard_dry_run import urn as urn_fmt
from integrations import slack

from . import approval, config, inputs, messages, plan_status

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "name not available"
# Shown once per contact, with the contact's name, instead of the generic sentence.
CONTACT_WARNINGS = ("address_incomplete", "phone_missing")


def _child_name_from_registry(registry, child_id) -> str | None:
    if registry is None or not child_id:
        return None
    try:
        return registry.child_name(str(child_id))
    except Exception:  # noqa: BLE001 - a missing name never stops the message
        return None


def gate_details(plan: dict, context: dict | None, registry=None) -> dict:
    """The personal details for the Gate A message, IN MEMORY ONLY: the child's name, the real start and end
    dates and the URN from the deal, each deal contact's name, email and whether their address and phone are
    complete, the Famly name of a child that already exists, and the names of the children the matched bill
    payer already bills (from the children registry). Never stored or logged."""
    context = context or {}
    deal = context.get("deal") or {}
    contacts = {}
    for contact in context.get("contacts") or []:
        props = getattr(contact, "props", None) or {}
        name = (inputs.full_name(props.get("firstname"), props.get("lastname"))
                or " ".join(str(props.get(k) or "").strip() for k in ("firstname", "lastname")).strip())
        contacts[str(contact.hubspot_id)] = {
            "name": name or None,
            "email": contact.email or "",
            "address": bool(getattr(contact, "has_complete_address", True)),
            "billPayer": bool(getattr(contact, "is_bill_payer", False)),
            "phone": getattr(contact, "phone_source", "none") != "none",
        }
    try:
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
    except ValueError:
        start = None
    try:
        end, _defaulted = derive.resolve_end_date(deal)
    except ValueError:
        end = None
    from actions.registry.match import deal_child_name
    child = plan.get("child") or {}
    bill_payer = plan.get("billPayer") if isinstance(plan.get("billPayer"), dict) else {}
    return {
        "childName": deal_child_name(deal) or None,
        "start": start,
        "end": end,
        "urn": str(deal.get("child_urn") or "").strip(),
        "contacts": contacts,
        "famlyChildName": _child_name_from_registry(registry, child.get("famlyChildId")),
        "otherChildren": [_child_name_from_registry(registry, cid) or f"child {cid}"
                          for cid in bill_payer.get("otherChildIds") or []],
    }


def bill_payer_question(plan: dict) -> bool:
    """True when the bill payer's email matched an existing payer: Gate A asks same / siblings / different."""
    section = plan.get("billPayer")
    return (plan.get("scope") != "child_only" and isinstance(section, dict)
            and section.get("status") == "would_attach")


def _date(value) -> str | None:
    return slack.fmt_date(value) if value else None          # dd/mm/yyyy in every Slack message


def _others(details: dict) -> str:
    return " and ".join(details.get("otherChildren") or []) or "another child"


def _who(entry: dict | None, details: dict) -> str:
    if entry is None:
        return "The bill payer"
    person = (details.get("contacts") or {}).get(str(entry.get("hubspotContactId"))) or {}
    return person.get("name") or f"contact {entry.get('hubspotContactId')}"


def _famly_child(plan: dict, details: dict) -> str:
    child_id = (plan.get("child") or {}).get("famlyChildId")
    return details.get("famlyChildName") or (f"Famly id {child_id}" if child_id else NOT_AVAILABLE)


def _urn_line(plan: dict, details: dict) -> str | None:
    urn = plan.get("urn") or {}
    status = urn.get("status")
    if status == urn_fmt.ALLOCATED:
        return f"Child URN: to be created ({urn.get('urn')})"
    if status == urn_fmt.WOULD_ALLOCATE:
        try:
            preview = urn_fmt.format_urn(urn.get("siteCode"), int(urn.get("nextNumber")))
        except (TypeError, ValueError):
            preview = "number not available"
        return f"Child URN: to be created ({preview}, not reserved yet)"
    if status == urn_fmt.EXISTING:
        return f"Child URN: already present ({details.get('urn') or 'on the deal'})"
    if details.get("urn"):
        return f"Child URN: already present ({details['urn']})"
    if status == urn_fmt.CHILD_EXISTS:
        return "Child URN: none on the deal (the child is already in Famly)"
    if status == urn_fmt.GENERATION_OFF_MISSING:
        return "Child URN: missing on the deal"
    return None


def _famly_child_ref(plan: dict, details: dict) -> str:
    """`*Name* (Famly id X)` for a child already in Famly, so staff can find it."""
    child_id = (plan.get("child") or {}).get("famlyChildId")
    name = details.get("famlyChildName")
    if name:
        return f"*{name}* (Famly id {child_id})" if child_id else f"*{name}*"
    return f"Famly id {child_id}" if child_id else NOT_AVAILABLE


def _child_action(plan: dict, details: dict, this_job_child_id: str | None = None) -> str:
    child = plan.get("child") or {}
    status = child.get("status")
    if status == "child_would_create":
        return ("Child does not exist in Famly and will be created (test child)." if child.get("testChild")
                else "Child does not exist in Famly and will be created.")
    if child.get("join"):
        how = ("confirmed as the same child by a person" if child["join"].get("decidedBy") == "person"
               else "exact name and date of birth match")
        return (f"Child already exists in Famly as the ad hoc child {_famly_child_ref(plan, details)} ({how}) and "
                "will be joined: the start date moved earlier if needed, the end date set and the Ad Hoc Only tag "
                "removed.")
    if status == "child_found":
        origin = ""
        if child.get("origin") == "created_by_pipeline":
            mine = this_job_child_id and child.get("famlyChildId") == this_job_child_id
            origin = " It was created by this job." if mine else " It was created by an earlier onboarding job."
        return (f"Child already exists in Famly: {_famly_child_ref(plan, details)}.{origin} Please check and "
                "update the creation status in HubSpot if all information is correct.")
    return messages.state(status if status in ("child_ambiguous", "child_unresolved") else "child_unresolved")


def _contact_actions(plan: dict, details: dict) -> list[str]:
    people = details.get("contacts") or {}
    contacts = plan.get("contacts") or []
    create = [e for e in contacts if e.get("status") == "would_create"]
    lines = ["Contacts to be created:" if create else "Contacts to be created: none"]
    for entry in create:
        email = (people.get(str(entry.get("hubspotContactId"))) or {}).get("email")
        lines.append(f"{_who(entry, details)} ({email})" if email else _who(entry, details))
    present = [e for e in contacts if e.get("status") == "present"]
    if present:
        lines.append("Already in Famly: " + ", ".join(
            _who(e, details) + (" (relation will be set to Parent)" if e.get("relation") == "would_add" else "")
            for e in present))
    for entry in contacts:
        if entry.get("status") in ("would_create", "present"):
            continue
        found = messages.lookup(plan_status.CONTACT_FLAG_CODES.get(entry.get("status"), entry.get("status") or ""))
        why = found[1] if found and found[1] else "it could not be checked yet."
        lines.append(f"{_who(entry, details)} will not be added: {why}")
    other = plan.get("childOtherContacts")
    if other and other.get("count"):
        lines.append(messages.state("child_other_contacts", k=other["count"]))
        if other.get("candidateIds"):
            lines.append(messages.state("child_other_contacts_candidates", ids=", ".join(other["candidateIds"])))
    return lines


def _bill_payer_actions(plan: dict, details: dict) -> list[str]:
    section = plan.get("billPayer")
    if not isinstance(section, dict) or section.get("status") == "skipped_stage":
        return []
    payer = _who(next((e for e in plan.get("contacts") or [] if e.get("isBillPayer")), None), details)
    status = section.get("status")
    if status == "would_create":
        lines = [f"{payer} will be created and linked as the bill payer."]
    elif status == "would_attach":
        lines = [f"Bill payer may already exist on {_others(details)}."]
    elif status == "present":
        lines = [f"{payer} is already the bill payer for this child."]
    else:
        found = messages.lookup(section.get("flag") or "")
        lines = [f"Bill payer will not be set up: {found[1] if found and found[1] else messages.UNKNOWN_SENTENCE}"]
    if section.get("differs") and status == "present":
        lines.append(messages.state("bill_payer_differs", fields=", ".join(section["differs"])))
    if section.get("recipient") == "contact_not_found":
        lines.append(messages.state("bill_payer_recipient_contact_not_found"))
    return lines


def _summary(plan: dict, details: dict) -> list[str]:
    writes = plan_status.planned_writes(plan)
    contacts = plan.get("contacts") or []
    items = []
    if writes["child"]:
        items.append(f"Child {details.get('childName') or NOT_AVAILABLE} to be created")
    if writes["join"]:
        items.append(f"Child {_famly_child(plan, details)} to be joined (dates and Ad Hoc Only tag)")
    items += [f"Contact {_who(e, details)} to be created" for e in contacts if e.get("status") == "would_create"]
    items += [f"Contact {_who(e, details)}: relation set to Parent" for e in contacts if e.get("relation") == "would_add"]
    section = plan.get("billPayer") if isinstance(plan.get("billPayer"), dict) else {}
    payer = _who(next((e for e in contacts if e.get("isBillPayer")), None), details)
    if section.get("status") == "would_create":
        items.append(f"Bill payer {payer} to be created and linked")
    elif section.get("status") == "would_attach":
        items.append(f"Bill payer {payer} may already exist, please confirm or deny")
    if section.get("recipient") == "will_add":
        items.append(f"{payer} linked to the bill payer to receive invoices")
    if not items:
        return [plan_status.nothing_sentence(plan)]
    return [f"• {item}" for item in items]


def _warnings(plan: dict, details: dict) -> list[str]:
    people = details.get("contacts") or {}
    lines = []
    for entry in plan.get("contacts") or []:
        person = people.get(str(entry.get("hubspotContactId")))
        if person is None:
            continue
        if not person["address"]:
            lines.append(f"{_who(entry, details)}'s address will not be added because the street, postcode or "
                         "city is missing.")
        if not person["phone"]:
            lines.append(f"{_who(entry, details)} has no phone number on the deal, so none will be added.")
    codes = [w for w in plan.get("warnings") or [] if not (people and w in CONTACT_WARNINGS)]
    shown = messages.classify(codes)
    return lines + shown[messages.NOT_DONE] + shown[messages.CHECK]


def gate_text(
    plan: dict,
    plan_hash: str,
    mode: str,
    plan_changed: bool = False,
    this_job_child_id: str | None = None,
    details: dict | None = None,
) -> str:
    """The Gate A message body for a plan: the child, site, dates and URN; *Actions*; *Summary*; *Warnings*; and,
    when the bill payer's email matched an existing payer, the same / siblings / different question.
    `details` is `gate_details(...)` (memory only); without it the names show as not available.
    `this_job_child_id` is the child this very job created (if any), to tell "created by this job" from "created
    by an earlier onboarding job"."""
    details = details or {}
    deal_id = plan["dealId"]
    child = plan.get("child") or {}
    dates = plan.get("dates") or {}
    child_name = details.get("childName") or NOT_AVAILABLE
    dry = "[DRY RUN] " if mode == config.MODE_DRY else ""

    lines = [f"{dry}Child: *{child_name}* ({deal_ref(deal_id)})"]
    if plan_changed:
        lines.append("⚠️ The plan changed since it was last approved; approval is needed again.")
    start = _date(details.get("start")) or ("on the deal" if dates.get("firstDayPresent") else "missing")
    if dates.get("lastDayDefaulted") and not (child.get("status") == "child_would_create" or child.get("join")):
        end = "none on the deal"          # an existing child: no end date is sent
    else:
        end = _date(details.get("end")) or ("on the deal" if dates.get("lastDayPresent") else "none")
        if dates.get("lastDayDefaulted"):
            end += " (default: 31 August after the 4th birthday)"
    lines.append(f"Site: {site_name(plan.get('siteCode'))} · Room: {plan.get('groupTitle')} · "
                 f"Start date: {start} · End date: {end}")
    urn_line = _urn_line(plan, details)
    if urn_line:
        lines.append(urn_line)

    lines += ["", "*Actions*", _child_action(plan, details, this_job_child_id)]
    if plan.get("scope") == "child_only":
        stage = (plan.get("stage") or {}).get("name") or "a child-only stage"
        lines.append("Child only: no contacts or bill payer at this stage.")
        lines.append(messages.state("scope_child_only", stage=stage))
    else:
        lines += _contact_actions(plan, details)
        lines += _bill_payer_actions(plan, details)
    lines += ["", "*Summary*"] + _summary(plan, details)
    lines += ["", "*Warnings*"] + (_warnings(plan, details) or ["None."])
    lines.append(f"Plan: `{plan_hash[:approval.HASH_PREFIX]}`")
    if bill_payer_question(plan):
        lines += ["", f"Bill payer for *{child_name}* has the same email as the bill payer on *{_others(details)}*. "
                      "Is this the same bill payer and are these children siblings?"]
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


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def done_text(deal_id: str, details: dict, child_created: bool, created_contact_ids, bill_payer_code: str | None,
              codes) -> str:
    """The Slack notice when an approved onboarding finished cleanly: one plain sentence with the child's, the
    new contacts' and the bill payer's NAMES (the Slack exception), an address line per parent whose address was
    not sent, any other warnings in plain words, and the deal link last. `details` is `gate_details(...)`."""
    from actions.onboard_dry_run import bill_payers as bp
    people = details.get("contacts") or {}
    child = details.get("childName") or NOT_AVAILABLE
    names = [(people.get(str(cid)) or {}).get("name") or f"contact {cid}" for cid in created_contact_ids]
    payer = next((p.get("name") for p in people.values() if p.get("billPayer")), None) or "the bill payer"
    others = details.get("otherChildren") or []
    line = f"✅ {child} {'created in' if child_created else 'already in'} Famly, with "
    line += f"contacts {_and(names)}" if names else "no new contacts"
    if bill_payer_code == bp.ATTACHED and others:
        line += f" and {payer} matched with bill payer from sibling {_and(others)}"
    elif bill_payer_code in (bp.CREATED, bp.ATTACHED, bp.PRESENT):
        line += f" and {payer} as bill payer"
    lines = [line + "."]
    for person in people.values():
        if not person.get("address"):
            lines.append(f"Please check {person.get('name') or 'a parent'}'s address in HubSpot as it was not "
                         "added to Famly due to incomplete information.")
    shown = messages.classify([c for c in codes if c != "address_incomplete"])
    lines += shown[messages.NOT_DONE] + shown[messages.CHECK]
    lines.append(f"HubSpot deal: {deal_ref(deal_id)}")
    return "\n".join(lines)


def siblings_text(deal_id: str, child_name: str | None, other_names, saved: bool) -> str:
    """After "Same bill payer and siblings": the children's NAMES (the Slack exception) and the deal link."""
    text = (f"👪 Siblings: *{child_name or NOT_AVAILABLE}* ({deal_ref(deal_id)}) and *{' and '.join(other_names)}* "
            "share a bill payer.")
    return text + (" Saved in the children registry." if saved
                   else " ⚠️ The link could not be saved in the children registry; ask an admin.")


def different_payer_text(deal_id: str, child_name: str | None, other_names) -> str:
    """After "Different bill payer" created a new payer with an email another payer already has."""
    return (f"⚠️ A new bill payer was created for *{child_name or NOT_AVAILABLE}* ({deal_ref(deal_id)}) with the "
            f"same email as the bill payer on *{' and '.join(other_names) or 'another child'}*. Please double "
            "check these are different people: two bill payers with one email may cause issues with billing.")


def admin_notice_text(deal_id: str, codes) -> str | None:
    """The one separate admin line for configuration problems, or None."""
    return messages.admin_text(deal_ref(deal_id), codes)


class SlackNotifier:
    """Posts through integrations.slack. Never raises."""

    def gate(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        """Post the Approve / Reject request; returns the message ref or None."""
        return slack.post_onboard_gate(text, approval.button_value(job_id, plan_hash, nonce))

    def gate_bill_payer(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        """The approval request with the bill payer buttons (same / siblings / different / Reject)."""
        return slack.post_onboard_gate_bill_payer(text, approval.button_value(job_id, plan_hash, nonce))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)

    def same_child(self, text: str, job_id: str, child_id: str, nonce: str) -> str | None:
        """The registry same-child question; returns the message ref or None."""
        return slack.post_onboard_same_child(text, approval.same_child_value(job_id, child_id, nonce))

    def hold_notice(self, text: str, job_id: str, nonce: str) -> bool:
        """A hold notice with a Retry button."""
        return slack.post_onboard_hold(text, approval.button_value(job_id, "", nonce)) is not None
