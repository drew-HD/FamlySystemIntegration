"""READ-ONLY facts for a deposit job: the deal, the child's start date, its one bill payer, its deposits.

Every gap is a code, never a guess:
  not ready yet (the job keeps waiting):  child_missing, plans_not_created
  holds (a person looks):                 stage_not_eligible, site_unmapped, start_date_missing,
                                          bill_payer_missing, bill_payer_ambiguous, bill_payer_scan_failed,
                                          deposit_exists, and every planner code
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from actions.onboard import summary_check
from actions.onboard_dry_run import bill_payers as payer_scan
from actions.onboard_dry_run import derive
from core.hubspot import PLAN_STATUS_CREATED

from . import famly

NOT_READY = ("child_missing", "plans_not_created")


@dataclass
class Facts:
    deal: dict
    stage: tuple[str, str]
    child_id: str | None = None
    site_code: str | None = None
    institution_id: str | None = None
    plan_start: date | None = None
    bill_payer_id: str | None = None
    deposits: list = field(default_factory=list)
    code: str | None = None          # first gap found; None = everything is there

    def child_name(self) -> str:
        return " ".join(str(self.deal.get(k) or "").strip() for k in ("child_first_name", "child_last_name")).strip()


def child_start(summary: Any) -> date | None:
    text = summary_check._date_part(summary_check._first(summary_check._child_node(summary),
                                                         summary_check.START_KEYS))
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def the_bill_payer(public, institution_id: str, child_id: str) -> tuple[str | None, str | None]:
    """(bill payer id, None) or (None, code): exactly one live payer must bill the child."""
    try:
        payers = payer_scan.scan_site(public, institution_id)
    except payer_scan.ScanFailed:
        return None, "bill_payer_scan_failed"
    billing = [p.id for p in payers if payer_scan.bills_the_child(p, child_id)]
    if not billing:
        return None, "bill_payer_missing"
    if len(billing) > 1:
        return None, "bill_payer_ambiguous"
    return billing[0], None


def gather(hub, rest, public, deal_id: str, stages: dict | None = None, *, need_plans: bool = True) -> Facts:
    deal = hub.get_deposit_deal(deal_id)
    stages = stages if stages is not None else hub.stage_names()
    pipeline, stage = stages.get(str(deal.get("dealstage") or ""), ("", ""))
    facts = Facts(deal=deal, stage=(pipeline, stage))
    if derive.norm_stage(pipeline) != "operations" or derive.is_left_nursery(stage):
        facts.code = "stage_not_eligible"
        return facts
    child_id = str(deal.get("famly_childid") or "").strip()
    if not child_id:
        facts.code = "child_missing"
        return facts
    facts.child_id = child_id
    if need_plans and str(deal.get("famly_plan_status") or "").strip() != PLAN_STATUS_CREATED:
        facts.code = "plans_not_created"
        return facts
    try:
        facts.site_code, facts.institution_id = derive.site_for_location(deal.get("location_of_interest"))
    except derive.Hold as exc:
        facts.code = exc.reason
        return facts
    facts.plan_start = child_start(rest.get(f"/v2/children/{child_id}/summary"))
    if facts.plan_start is None:
        facts.code = "start_date_missing"
        return facts
    facts.bill_payer_id, code = the_bill_payer(public, facts.institution_id, child_id)
    if code:
        facts.code = code
        return facts
    facts.deposits = famly.list_deposits(rest, facts.institution_id, child_id)
    return facts
