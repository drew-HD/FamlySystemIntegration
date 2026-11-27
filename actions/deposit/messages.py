"""Slack text for the deposit pipeline. Child names are allowed in Slack (with the deal link); never phone
numbers, addresses or dates of birth. Nothing here is logged."""

from datetime import date, timedelta

from actions.adhoc.messages import deal_link
from integrations.slack import fmt_date

from . import planner

HOLD_TEXT = {
    "stage_not_eligible": "The deal is not in the Operations pipeline (or it is Left Nursery).",
    "site_unmapped": "The deal's location is not Westminster, City or Canada Water.",
    "site_id_missing": "The site has no Famly id in the reference files.",
    "start_date_missing": "The child has no start date in Famly.",
    "bill_payer_missing": "No bill payer in Famly bills this child.",
    "bill_payer_ambiguous": "More than one bill payer in Famly bills this child.",
    "bill_payer_scan_failed": "Famly's bill payer list could not be read. Resume the job to try again.",
    "bill_payer_changed": "The child's bill payer in Famly changed since the deposit was approved.",
    "famly_ids_changed": "The child, site or bill payer in Famly changed since the approval message. Cancel and "
                         "enqueue again.",
    "deposit_exists": "The child already has a deposit in Famly. Check it by hand.",
    "deposit_list_changed": "The child's deposits in Famly changed (a deposit was added or removed by hand).",
    "deposit_readback_mismatch": "The deposit was sent but Famly's list does not show it as expected. Check by hand.",
    "deposit_outcome_unknown": "A deposit request did not finish. Check Famly before resuming.",
    "invoice_preview_mismatch": "The invoice preview total is not the deposit amount (other charges would be on "
                                "it, e.g. a sibling's or this child's fees). Send the invoice by hand.",
    "invoice_outcome_unknown": "An invoice request did not finish. Check Famly before resuming.",
    "invoice_before_deposit": "An invoice was due before its deposit was added.",
    "plan_changed": "The deposit amounts on the deal changed after approval. Cancel and enqueue again.",
    "deal_not_allowed": "Live mode, but the deal is not in DEPOSIT_ALLOWED_DEAL_IDS.",
    "waited_too_long": "The child, bill payer or plans did not appear in Famly in time.",
    "slack_post_failed": "The approval message could not be posted to Slack.",
    planner.AMOUNT_MISSING: "The deal has no Total Deposit Amount.",
    planner.AMOUNT_INVALID: "The deal's deposit amount is not a positive amount in pounds and pence.",
    planner.PREFERENCE_MISSING: "The deal has no Deposit Preference.",
    planner.PREFERENCE_UNKNOWN: "The deal's Deposit Preference is not Full Deposit Payment or 50 / 25 / 25.",
    planner.INSTALLMENT_MISSING: "An installment amount is missing on the deal.",
    planner.INSTALLMENTS_MISMATCH: "The installments do not add up to the Total Deposit Amount (or the first is "
                                   "not half).",
    planner.START_DATE_MISSING: "The child has no start date in Famly.",
}


def money(amount) -> str:
    return f"£{amount:,.2f}"


def _who(name: str, deal_id: str) -> str:
    return f"{name or 'Child'} (deal {deal_link(deal_id)})"


def gate_text(name: str, deal_id: str, preference: str, installments: list, dry: bool) -> str:
    lines = [f"*Deposit for {_who(name, deal_id)}*", f"{preference}, total {money(planner.total_of(installments))}"]
    for i in installments:
        on = fmt_date(i.on)
        if i.number == 1:
            lines.append(f"• {money(i.amount)} added and invoiced on approval")
        else:
            added = fmt_date(i.on - timedelta(days=1))
            invoice = (f"own invoice on {fmt_date(i.invoice_on)}" if i.invoice_on
                       else "on Famly's regular invoice that month")
            lines.append(f"• {money(i.amount)} dated {on}, added on {added}, {invoice}")
    if dry:
        lines.append("_Dry run: Approve writes nothing._")
    return "\n".join(lines)


def hold_text(name: str, deal_id: str, code: str) -> str:
    return (f"⚠️ Deposit held for {_who(name, deal_id)}: {HOLD_TEXT.get(code, code)}\n"
            f"Fix it, then `deposit-status --resume JOB` or `--cancel JOB`.")


def done_text(name: str, deal_id: str, what: str) -> str:
    return f"💷 Deposit for {_who(name, deal_id)}: {what}"


def as_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None
