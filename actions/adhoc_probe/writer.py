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
UPSERT_DETAIL_QUERY = HERE / "upsert_purchase_detail.graphql"
DELETE_QUERY = HERE / "delete_purchase.graphql"
END_DATE_OP, UPSERT_OP, DELETE_OP = "EditChildActiveDates", "UpsertPurchase", "DeletePurchase"

MAX_WRITES = 12
TAG_TITLE = "Ad Hoc Only"
STEPS = ("end-date", "tag", "booking", "create-variants", "left-booking", "discount")
VARIANTS = {"HDCW_NOEND": "no end date at all", "HDCW_LEFT": "enrolment already ended (2026-06-30)"}
LEFT_END = "2026-06-30"
SESSION_TITLE = "Morning"            # the plain ad hoc session; the "(F) ..." variants carry no ad hoc price
BOOKING_DAY = "MONDAY"

# Pricing group per site and age band, from Drew (2026-11-24). Prices are the same in both groups today.
# HDCW: eebac1d0 is under 3 (stated); d31d08ee is the site's other group (taken as over 3).
PRICING_GROUPS = {
    "HDCW": {"under3": "eebac1d0-8fb1-4c1c-a854-28ab4ed6873b",
             "over3": "d31d08ee-d4e1-468c-ada3-44ff7ae67ad8"},
    "HDCITY": {"under3": "18b50271-fe44-45a5-acb6-370795e2edf5",
               "over3": "78d46103-bf26-44d0-9959-9ca813ea4139"},
    "HDWEST": {"under3": "3be14e3c-7401-4394-b1f7-468b96eac0ec",
               "over3": "3773609b-ff7f-46ac-a147-53954aeb4268"},
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

    def __init__(self, rest, gql, children: dict[str, str], public=None):
        self.rest, self.gql, self.public = rest, gql, public
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

    def create_variant(self, payload: dict) -> str:
        """Create ONE test child (isTestChild true enforced by the seeder's guard), idempotent by external id."""
        seed.assert_test_payload(payload)
        found = self.public.execute(query_path=seed.LOOKUP_QUERY, operation_name=seed.LOOKUP_OP,
                                    variables={"foreignId": payload["externalId"], "system": "URN"})
        rows = found["data"]["children"]["listByExternalSystemReference"]["result"]
        if rows:
            if len(rows) > 1 or rows[0]["id"] in self.children.values():
                raise WriteRefused("variant already exists or is ambiguous; not creating another")
            raise WriteRefused(f"variant already exists ({rows[0]['id']}); add it to the test children store")
        self._spend()
        body = self.public.execute(query_path=seed.CREATE_QUERY, operation_name=seed.CREATE_OP,
                                   variables={"children": [payload]})
        return body["data"]["children"]["create"][0]["id"]

    def upsert(self, child_id: str, purchase: dict, detail: bool = False) -> Any:
        self._guard(child_id)
        if purchase.get("childId") != child_id or purchase.get("productBookings") != []:
            raise WriteRefused("refusing: purchase is not for this test child, or carries product bookings")
        self._spend()
        body = self.gql.execute(query_path=UPSERT_DETAIL_QUERY if detail else UPSERT_QUERY,
                                operation_name=UPSERT_OP, variables={"purchases": [purchase]})
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


def build_purchase(child_id: str, session_body: dict, group_id: str, day: date,
                   title: str = SESSION_TITLE) -> tuple[dict, str]:
    """(PurchaseInput, session id) for one plain `title` booking on `day`, priced from the lookup."""
    items = (((session_body or {}).get("data") or {}).get("finance") or {}).get("revenueItems") or {}
    sessions = [v for v in items.get("sessionVersions") or []
                if v.get("title") == title and v.get("type") == "StandaloneSession"]
    if len(sessions) != 1:
        raise WriteRefused(f"session_{'not_found' if not sessions else 'ambiguous'} ({title})")
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


def step_booking(w: Writer, site: str, child_id: str, out, today: date, discount: str = "0",
                 pricing_site: str | None = None) -> None:
    day = reads.first_monday_after(today)
    child = w.summary(child_id)["child"]
    birthday = date.fromisoformat(str(child.get("birthday"))[:10])
    group_id = pricing_group_for(pricing_site or site, birthday, day)
    lookup = w.gql.execute(query_path=reads.SESSIONS_QUERY, operation_name=reads.SESSIONS_OP,
                           variables={"childIds": [child_id], "validOnDate": day.isoformat()})
    purchase, session_id = build_purchase(child_id, lookup, group_id, day)
    purchase["discountAmount"] = discount
    out(f"  {site}: booking {SESSION_TITLE} on {day} ({day.strftime('%A')}), group {group_id[:8]}, "
        f"price {purchase['sessionBookings'][0]['bookedPrice']}, discountAmount sent {discount!r}")
    preview = w.gql.execute(query_path=reads.CONFLICTS_QUERY, operation_name=reads.CONFLICTS_OP,
                            variables={"childId": child_id, "purchases": [purchase], "date": day.isoformat()})
    result = (((preview.get("data") or {}).get("finance") or {}).get("purchases") or {}).get("previewUpdate") or {}
    # The preview lists the submitted session itself as INPUT_PURCHASE ("this is yours") and the sessions it
    # blocks as ANOTHER_INPUT_PURCHASE. Any OTHER kind on OUR session (already booked, closed, ...) is a conflict.
    own_kinds = {u.get("conflictKind") for u in result.get("unbookableSessions") or []
                 if u.get("sessionId") == session_id}
    if result.get("newPurchaseConflictMessages") or own_kinds - {"INPUT_PURCHASE"}:
        raise WriteRefused("conflict_preview_not_clean")
    body = w.upsert(child_id, purchase, detail=discount != "0")
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


def variant_payload(label: str, site_id: str, group_id: str) -> dict:
    """A test child like create-test-children makes, but HDCW_NOEND has NO lastDay and HDCW_LEFT has a past one."""
    payload = seed.make_child_input(label.replace("_", "-"), site_id, group_id)
    relation = payload["sitesRelation"][0]
    if label == "HDCW_NOEND":
        relation.pop("lastDay")
    else:
        relation["lastDay"] = LEFT_END
    return payload


def step_create_variants(w: Writer, out, store: Path) -> dict[str, str]:
    site_id, group_id = seed.resolve_site("HDCW")
    created = {}
    for label, what in VARIANTS.items():
        if label in w.children:
            out(f"  {label}: already in the store ({w.children[label]}); not created again")
            continue
        try:
            child_id = w.create_variant(variant_payload(label, site_id, group_id))
        except WriteRefused as exc:
            out(f"  {label}: STOPPED, {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "errors", None) or getattr(exc, "body", None) or str(exc)
            out(f"  {label} ({what}): create REJECTED, {type(exc).__name__}: {json.dumps(body, default=str)[:500]}")
            continue
        w.children[label] = child_id
        created[label] = child_id
        after = w.summary(child_id)["child"]
        out(f"  {label} ({what}): created {child_id}; summary isTestChild={after.get('isTestChild')} "
            f"startDate={after.get('localStartDate')!r} endDate={after.get('endDate')!r}")
    if created:
        known = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        known.update(created)
        store.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
        out(f"  ids saved to {store} (ids only)")
    return created


def load_variants(rest, store: Path) -> dict[str, str]:
    """The extra test children (HDCW_NOEND / HDCW_LEFT) from the store, kept only if verified isTestChild."""
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = {}
    for label in VARIANTS:
        child_id = data.get(label)
        if isinstance(child_id, str):
            try:
                if seed.is_test_child(rest.get(f"/v2/children/{child_id}/summary")):
                    ok[label] = child_id
            except Exception:  # noqa: BLE001
                pass
    return ok


def run(confirm: bool, steps: list[str], booking_site: str, rest, gql, out: Callable[[str], None] = print,
        today: date | None = None, store_path: Path | None = None, public=None,
        discount: str = "10") -> dict:
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
    store = store_path or seed.STORE_PATH
    children.update(load_variants(rest, store))
    w = Writer(rest, gql, children, public)
    out(f"CAPS: at most {MAX_WRITES} write requests; test children only: "
        + ", ".join(f"{s}={c}" for s, c in children.items()))
    outcome: dict[str, dict[str, str]] = {}
    for step in steps:
        out(f"=== step {step} ===")
        if step == "create-variants":
            if public is None:
                raise WriteRefused("create-variants needs the public client")
            step_create_variants(w, out, store)
            outcome["create-variants"] = {k: w.children.get(k, "missing") for k in VARIANTS}
            continue
        if step == "left-booking":
            targets = {"HDCW_LEFT": w.children.get("HDCW_LEFT")}
        else:
            targets = {booking_site: children.get(booking_site)} if step in ("booking", "discount") else children
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
                elif step == "left-booking":
                    step_booking(w, site, child_id, out, today or date.today(), pricing_site="HDCW")
                elif step == "discount":
                    step_booking(w, site, child_id, out, today or date.today(), discount=discount)
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
