"""adhoc-probe-write: the WRITE stage of the ad hoc probe, on the TEST children only.

A narrow exception to "never call a write against Famly outside tests", like the other probe commands:
it only ever touches the three AATest children from create-test-children. Steps (each chosen with --step):

  end-date  remove the child's end date (EditChildActiveDates with startDate only), so Famly's automatic
            end date applies. Skipped if the child already has none.
  tag       assign the site's "Ad Hoc Only" tag (POST /v2/tags/<id>/assign). The tag id is looked up per
            site (GET /v2/tags?institutionId=); exactly one live tag with that title or the site is skipped.
  booking   ONE booking cycle on one test child: look up the plain "Morning" session and its price for the
            child's pricing group (under 3 / over 3 by age), preview conflicts, UpsertPurchase one Monday,
            then DeletePurchase exactly the purchase this run created.

Guards: `--confirm`; at least one --step; every child id must be a verified test child (isTestChild true in
its summary, re-read right before each write); at most MAX_WRITES writes; a delete only of purchase ids
created in this run; a missing or unmapped pricing group, a missing or non-positive price, or any conflict
is a hard stop for that step (nothing guessed). Nothing here runs from a web action, queue or cron.
"""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from integrations import catalogue

from . import runner as reads
from actions.testchild_seed import runner as seed

HERE = Path(__file__).parent
END_DATE_QUERY = HERE / "edit_active_dates.graphql"
UPSERT_QUERY = HERE / "upsert_purchase.graphql"
DELETE_QUERY = HERE / "delete_purchase.graphql"
END_DATE_OP, UPSERT_OP, DELETE_OP = "EditChildActiveDates", "UpsertPurchase", "DeletePurchase"

MAX_WRITES = 8
TAG_TITLE = "Ad Hoc Only"
STEPS = ("end-date", "tag", "booking")
SESSION_TITLE = "Morning"            # the plain ad hoc session; the "(F) ..." variants carry no ad hoc price
BOOKING_DAY = "MONDAY"

# Pricing group per site and age band (Drew, 2026-11-24: eebac1d0-... is under 3). Prices are the same in
# both groups today. HDCW over-3 is the site's OTHER group (inferred, to be confirmed). HDCITY and HDWEST
# are not mapped yet, so a booking there is a hard stop.
PRICING_GROUPS = {
    "HDCW": {"under3": "eebac1d0-8fb1-4c1c-a854-28ab4ed6873b",
             "over3": "d31d08ee-d4e1-468c-ada3-44ff7ae67ad8"},
}


class WriteRefused(Exception):
    """A guard failed. Nothing further was written for that step."""


def age_band(birthday: date, on: date) -> str:
    """'under3' until the third birthday, 'over3' from it (a 3-year-old counts as over 3)."""
    try:
        third = birthday.replace(year=birthday.year + 3)
    except ValueError:                      # 29 February
        third = birthday.replace(year=birthday.year + 3, day=28) + timedelta(days=1)
    return "under3" if on < third else "over3"


def pricing_group_for(site: str, birthday: date, on: date) -> str:
    group = (PRICING_GROUPS.get(site) or {}).get(age_band(birthday, on))
    if not group:
        raise WriteRefused(f"pricing_group_unmapped for {site}/{age_band(birthday, on)}")
    return group


def purchase_ids(body: Any) -> list[str]:
    """Ids of purchase objects in an UpsertPurchase response (a dict with an id AND sessionBookings)."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("id") and "sessionBookings" in node:
                found.append(str(node["id"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk((body or {}).get("data"))
    return list(dict.fromkeys(found))


class Writer:
    """The ONLY write paths."""

    def __init__(self, rest, gql, children: dict[str, str]):
        self.rest, self.gql = rest, gql
        self.children = dict(children)               # site -> verified test child id
        self.writes = 0
        self.created_purchases: set[str] = set()

    def _spend(self) -> None:
        if self.writes >= MAX_WRITES:
            raise WriteRefused(f"write cap ({MAX_WRITES}) reached")
        self.writes += 1

    def summary(self, child_id: str) -> dict:
        return self.rest.get(f"/v2/children/{child_id}/summary")

    def _guard(self, child_id: str) -> dict:
        """The child must be one of the verified test children AND still say isTestChild true."""
        if child_id not in self.children.values():
            raise WriteRefused("refusing: not one of the verified test children")
        summary = self.summary(child_id)
        if not seed.is_test_child(summary):
            raise WriteRefused("refusing: summary does not say isTestChild true")
        return summary

    def remove_end_date(self, child_id: str, start_date: str) -> Any:
        self._guard(child_id)
        dates = {"startDate": start_date}
        assert "endInfo" not in dates                # removal only; this probe never SETS an end date
        self._spend()
        return self.gql.execute(query_path=END_DATE_QUERY, operation_name=END_DATE_OP,
                                variables={"childId": child_id, "childActiveDates": dates, "isParent": False})

    def assign_tag(self, child_id: str, tag_id: str) -> Any:
        self._guard(child_id)
        self._spend()
        return self.rest.post(f"/v2/tags/{tag_id}/assign", json_body={"childId": child_id})

    def upsert(self, child_id: str, purchase: dict) -> Any:
        self._guard(child_id)
        if purchase.get("childId") != child_id or purchase.get("productBookings") != []:
            raise WriteRefused("refusing: purchase is not for this test child, or carries product bookings")
        self._spend()
        body = self.gql.execute(query_path=UPSERT_QUERY, operation_name=UPSERT_OP,
                                variables={"purchases": [purchase]})
        self.created_purchases.update(purchase_ids(body))
        return body

    def delete(self, child_id: str, ids: list[str]) -> Any:
        self._guard(child_id)
        if not ids or any(i not in self.created_purchases for i in ids):
            raise WriteRefused("refusing to delete a purchase this run did not create")
        self._spend()
        return self.gql.execute(query_path=DELETE_QUERY, operation_name=DELETE_OP,
                                variables={"purchaseIds": list(ids)})


# --------------------------------------------------------------------------- #
# The steps
# --------------------------------------------------------------------------- #
def step_end_date(w: Writer, site: str, child_id: str, out) -> None:
    child = w.summary(child_id)["child"]
    if not child.get("endDate") and not child.get("localEndDate"):
        out(f"  {site}: already has no end date; nothing to do")
        return
    start = str(child.get("localStartDate") or "")[:10]
    if not start:
        raise WriteRefused("no start date to keep")
    w.remove_end_date(child_id, start)
    after = w.summary(child_id)["child"]
    out(f"  {site}: end date removed; summary now endDate={after.get('endDate')!r} "
        f"startDate={after.get('localStartDate')!r}")


def find_tag(rest, institution_id: str) -> str:
    tags = rest.get("/v2/tags", params={"institutionId": institution_id})
    rows = tags if isinstance(tags, list) else []
    live = [t for t in rows if isinstance(t, dict) and t.get("title") == TAG_TITLE and not t.get("deletedAt")
            and t.get("institutionId") == institution_id and t.get("tagId")]
    if len(live) != 1:
        raise WriteRefused(f"tag_{'not_found' if not live else 'ambiguous'} ({len(live)} match(es))")
    return str(live[0]["tagId"])


def step_tag(w: Writer, site: str, child_id: str, out) -> None:
    entry = catalogue.load_sites().get(site)
    institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
    if not institution_id:
        raise WriteRefused("site_id_missing")
    tag_id = find_tag(w.rest, institution_id)
    have = {t.get("tagId") for t in w.summary(child_id).get("tags") or [] if isinstance(t, dict)}
    if tag_id in have:
        out(f"  {site}: already tagged ({tag_id})")
        return
    w.assign_tag(child_id, tag_id)
    now = {t.get("tagId") for t in w.summary(child_id).get("tags") or [] if isinstance(t, dict)}
    out(f"  {site}: tag {tag_id} assigned; present on summary = {tag_id in now}")


def build_purchase(child_id: str, session_body: dict, group_id: str, day: date) -> tuple[dict, str]:
    """(PurchaseInput, session id) for one plain Morning booking on `day`, priced from the lookup."""
    items = (((session_body or {}).get("data") or {}).get("finance") or {}).get("revenueItems") or {}
    sessions = [v for v in items.get("sessionVersions") or []
                if v.get("title") == SESSION_TITLE and v.get("type") == "StandaloneSession"]
    if len(sessions) != 1:
        raise WriteRefused(f"session_{'not_found' if not sessions else 'ambiguous'} ({SESSION_TITLE})")
    session = sessions[0]
    day_name = day.strftime("%A").upper()
    avail = [a for a in session.get("sessionAvailabilities") or [] if a.get("day") == day_name]
    if len(avail) != 1 or not avail[0].get("sessionAvailabilityId"):
        raise WriteRefused(f"availability_missing for {day_name}")
    prices = [p for p in session.get("pricing") or [] if p.get("pricingGroupId") == group_id]
    if len(prices) != 1:
        raise WriteRefused("price_missing_for_pricing_group")
    try:
        price = Decimal(str(prices[0].get("adHocPrice")))
    except (InvalidOperation, TypeError):
        raise WriteRefused("price_not_a_number") from None
    if price <= 0:
        raise WriteRefused("price_not_positive")
    purchase = {"childId": child_id, "discountAmount": "0", "productBookings": [],
                "sessionBookings": [{"bookedPrice": f"{price:.2f}", "pricingGroupId": group_id,
                                     "purchaseDate": day.isoformat(),
                                     "sessionAvailabilityId": avail[0]["sessionAvailabilityId"]}]}
    return purchase, str(session.get("sessionId") or "")


def step_booking(w: Writer, site: str, child_id: str, out, today: date) -> None:
    day = reads.first_monday_after(today)
    child = w.summary(child_id)["child"]
    birthday = date.fromisoformat(str(child.get("birthday"))[:10])
    group_id = pricing_group_for(site, birthday, day)
    lookup = w.gql.execute(query_path=reads.SESSIONS_QUERY, operation_name=reads.SESSIONS_OP,
                           variables={"childIds": [child_id], "validOnDate": day.isoformat()})
    purchase, session_id = build_purchase(child_id, lookup, group_id, day)
    out(f"  {site}: booking {SESSION_TITLE} on {day} ({day.strftime('%A')}), group {group_id[:8]}, "
        f"price {purchase['sessionBookings'][0]['bookedPrice']}")
    preview = w.gql.execute(query_path=reads.CONFLICTS_QUERY, operation_name=reads.CONFLICTS_OP,
                            variables={"childId": child_id, "purchases": [purchase], "date": day.isoformat()})
    result = (((preview.get("data") or {}).get("finance") or {}).get("purchases") or {}).get("previewUpdate") or {}
    # The preview lists the submitted session itself as INPUT_PURCHASE ("this is yours") and the sessions it
    # blocks as ANOTHER_INPUT_PURCHASE. Any OTHER kind on OUR session (already booked, closed, ...) is a conflict.
    own_kinds = {u.get("conflictKind") for u in result.get("unbookableSessions") or []
                 if u.get("sessionId") == session_id}
    if result.get("newPurchaseConflictMessages") or own_kinds - {"INPUT_PURCHASE"}:
        raise WriteRefused("conflict_preview_not_clean")
    body = w.upsert(child_id, purchase)
    ids = purchase_ids(body)
    out(f"  {site}: UpsertPurchase created {len(ids)} purchase(s): {', '.join(ids) or 'NONE FOUND IN RESPONSE'}")
    out(f"  {site}: response: {json.dumps(body.get('data'))[:500]}")
    if not ids:
        raise WriteRefused("no purchase id in the response; CHECK THE TEST CHILD'S BOOKINGS BY HAND")
    try:
        deleted = w.delete(child_id, ids)
        out(f"  {site}: DeletePurchase response: {json.dumps(deleted.get('data'))[:300]}")
    except Exception as exc:  # noqa: BLE001
        out(f"  {site}: DELETE FAILED ({type(exc).__name__}); DELETE THESE PURCHASES BY HAND: {', '.join(ids)}")
        raise


def run(confirm: bool, steps: list[str], booking_site: str, rest, gql, out: Callable[[str], None] = print,
        today: date | None = None, store_path: Path | None = None) -> dict:
    if not confirm:
        raise WriteRefused("refusing without --confirm; nothing was written")
    if not steps or any(s not in STEPS for s in steps):
        raise WriteRefused(f"give at least one --step of {', '.join(STEPS)}")
    if booking_site not in seed.SITES:
        raise WriteRefused("unknown --booking-site")
    try:
        children = reads.verify_test_children(rest, reads.load_test_children(store_path))
    except reads.ProbeRefused as exc:
        raise WriteRefused(str(exc)) from None
    w = Writer(rest, gql, children)
    out(f"CAPS: at most {MAX_WRITES} write requests; test children only: "
        + ", ".join(f"{s}={c}" for s, c in children.items()))
    outcome: dict[str, dict[str, str]] = {}
    for step in steps:
        out(f"=== step {step} ===")
        targets = {booking_site: children.get(booking_site)} if step == "booking" else children
        for site, child_id in targets.items():
            if not child_id:
                outcome.setdefault(step, {})[site] = "no_verified_test_child"
                out(f"  {site}: SKIPPED (no verified test child)")
                continue
            try:
                if step == "end-date":
                    step_end_date(w, site, child_id, out)
                elif step == "tag":
                    step_tag(w, site, child_id, out)
                else:
                    step_booking(w, site, child_id, out, today or date.today())
                outcome.setdefault(step, {})[site] = "ok"
            except WriteRefused as exc:
                outcome.setdefault(step, {})[site] = f"stopped: {exc}"
                out(f"  {site}: STOPPED, {exc}")
            except Exception as exc:  # noqa: BLE001 - report and carry on to the next site
                outcome.setdefault(step, {})[site] = f"error: {type(exc).__name__}"
                body = getattr(exc, "errors", None) or getattr(exc, "body", None) or str(exc)
                out(f"  {site}: ERROR {type(exc).__name__}: {json.dumps(body, default=str)[:600]}")
    return {"outcome": outcome, "writeRequests": w.writes}
