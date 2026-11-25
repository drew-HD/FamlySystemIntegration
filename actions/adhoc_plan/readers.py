"""The READ-ONLY wiring the ad hoc planner needs, shared by `adhoc-dry-run` and the ad hoc worker.

Every Famly and HubSpot client here is wrapped read-only (ReadOnlyHubSpot / ReadOnlyGraphQL / ReadOnlyRest from
onboard_audit), so nothing built on it can write. The pipeline's WRITES will use separate, explicit clients.
"""

from datetime import date

from actions.adhoc_audit import runner as adhoc
from actions.adhoc_probe import runner as reads
from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import derive, links

from . import planner, urn_plan

NOT_AD_HOC = "not_ad_hoc_pipeline"
DEAL_NOT_FOUND = "deal_not_found"


def allocated_urn_for_deal(deal_id: str) -> str | None:
    """The URN already reserved for this deal in the onboard store (read only), as a string, or None.

    The store holds the URN SUFFIX ("CW"), not the site code, exactly as `UrnService.own` reads it."""
    from actions.onboard import store as onboard_store
    from actions.onboard_dry_run import urn as urn_fmt
    held = onboard_store.urn_allocation_for_deal(str(deal_id))
    return urn_fmt.format_urn(held[0], held[1]) if held else None


def resume_contacts_for_deal(deal_id: str, child_id: str) -> bool:
    """True when an earlier job of this deal created this child and recorded NO contact outcome at all (it stopped
    before contacts). A job with SOME contact outcomes is not resumed: a person decides."""
    from actions.adhoc import store
    earlier = store.earlier_creates_for_deal(str(deal_id), str(child_id), "")
    return bool(earlier) and not any(str(o).startswith("contact:") for job in earlier for o in job.outcomes or [])


class Readers:
    def __init__(self, today: date | None = None):
        from actions.urn_lookup import runner as urn_runner
        from core.client import GraphQLClient
        from core.rest_client import RestClient

        self._urn = urn_runner
        self.today = today
        self.hubspot = audit.ReadOnlyHubSpot()
        self.public = audit.ReadOnlyGraphQL(urn_runner._public_client())
        self.internal = audit.ReadOnlyGraphQL(GraphQLClient())
        self.rest = audit.ReadOnlyRest(RestClient())
        self._labels = None
        try:
            self._test_children = reads.load_test_children()
        except reads.ProbeRefused:
            self._test_children = {}

    def famly_index(self) -> audit.Index:
        self._index = audit.Index(audit.fetch_children(self.public, self._urn._all_institution_ids()))
        return self._index

    def child_name(self, child_id: str) -> str | None:
        """The child's Famly name from the last Famly pull. Held in memory only; for the Slack match question."""
        child = getattr(self, "_index", None) and self._index.children.get(child_id)
        return (child.full_name or None) if child else None

    def child_details(self, child_id: str) -> dict | None:
        """The child's Famly name, date of birth and URN from the last Famly pull (memory only), for its children
        registry row. None if the child was not in that pull."""
        child = getattr(self, "_index", None) and self._index.children.get(child_id)
        if not child:
            return None
        return {"name": child.full_name or "", "dob": child.birthday or "", "urn": child.external_id or ""}

    def deps(self) -> planner.Deps:
        from actions.read_child_plans import runner as plans_runner

        def read_sessions(child_id, day):
            return self.internal.execute(query_path=reads.SESSIONS_QUERY, operation_name=reads.SESSIONS_OP,
                                         variables={"childIds": [child_id], "validOnDate": day.isoformat()})

        def preview(child_id, purchases, day):
            body = self.internal.execute(query_path=reads.CONFLICTS_QUERY, operation_name=reads.CONFLICTS_OP,
                                         variables={"childId": child_id, "purchases": purchases,
                                                    "date": day.isoformat()})
            return (((body.get("data") or {}).get("finance") or {}).get("purchases") or {}).get("previewUpdate")

        from actions.adhoc import config as adhoc_config
        from actions.onboard.urn_alloc import UrnService
        from actions.onboard_dry_run import bill_payers as payer_scan
        from actions.onboard_dry_run.famly_reads import FamlyReads
        from actions.registry import lookup as registry_lookup

        famly = FamlyReads(self.public)                  # read-only wrapped; the roster is read once per plan
        live = adhoc_config.mode() == adhoc_config.MODE_LIVE
        return planner.Deps(
            has_plan=lambda cid: bool(plans_runner.run(cid, client=self.rest).plans),
            read_sessions=read_sessions, preview=preview,
            price_child_for_site=lambda site: self._test_children.get(site),
            urn=lambda deal_id, site: urn_plan.urn_section(UrnService(self.hubspot, famly), deal_id, site, live),
            bill_payers=famly.bill_payers if payer_scan.enabled() else None,
            allocated_urn=allocated_urn_for_deal, resume_contacts=resume_contacts_for_deal,
            registry=registry_lookup.candidates, registry_deals=registry_lookup.operations_deals)

    def deal_and_contacts(self, deal_id: str):
        """The deal and contacts from the SAME read that produced the last plan for this deal (memory only), so the
        write phase never builds a child from a second, possibly different, read. (None, None) if there was none."""
        return getattr(self, "_context", {}).pop(str(deal_id), (None, None))

    def plan_deal(self, deal_id: str, index: audit.Index | None = None, confirmed_child_id: str | None = None,
                  force_new: bool = False) -> dict:
        """The plan for one deal, or {"error": code} if it is not there or not an Ad Hoc deal."""
        stages = self.hubspot.stage_names()
        deals = adhoc.deals_by_id(self.hubspot, [str(deal_id)])
        if not deals:
            return {"error": DEAL_NOT_FOUND}
        deal = deals[0]
        pipeline, stage = stages.get(str(deal.get("dealstage")), ("", ""))
        if derive.norm_stage(pipeline) != adhoc.PIPELINE:
            return {"error": NOT_AD_HOC, "stage": stage or None}
        if self._labels is None:
            self._labels = links.load_labels(self.hubspot)
        try:
            contacts = links.deal_contacts(self.hubspot, self._labels, str(deal["id"]))
        except Exception:  # noqa: BLE001
            contacts = None
        self._context = {**getattr(self, "_context", {}), str(deal_id): (deal, contacts)}
        plan = planner.build_plan(deal, stage or None, contacts, index or self.famly_index(), self.deps(),
                                  self.today or date.today(), confirmed_child_id=confirmed_child_id,
                                  force_new=force_new)
        if contacts is None:
            plan["flags"].append("contacts_not_readable")
        return plan
