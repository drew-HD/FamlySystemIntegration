"""The ONLY Famly writes the ad hoc worker makes for an EXISTING child (new children: step 3b), each guarded:

  book      UpsertPurchase with the exact purchase of the APPROVED plan (ids and prices fixed at approval).
  tag       POST /v2/tags/<id>/assign (the site's "Ad Hoc Only" tag), only for an ad hoc only child.
  end date  EditChildActiveDates with startDate only (removes the end date), only for an ad hoc only child.

There is NO delete or update of a booking anywhere in this package. NEVER RETRY: each function sends ONE request and
classifies the outcome, like actions/onboard/famly_writes.py:

  * `BookingRejected`  Famly refused it and nothing ran (GraphQL errors with no data, or an HTTP 4xx).
  * `BookingUnknown`   it may or may not have landed (a timeout, an HTTP 5xx, errors that came WITH data): the caller
                       ends the job `needs_review` for a person to look; it is never sent again. (A later Retry plans
                       again, and Famly's conflict preview then reports a session that is already booked.)
The tag and the end date are administrative: after a request that did not answer cleanly they are SETTLED BY A READ of
the child's summary, never re-sent. Error text never leaves here: only short codes.
"""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.client import GraphQLError, GraphQLHTTPError

HERE = Path(__file__).parent
UPSERT_QUERY = HERE / "upsert_purchase.graphql"
END_DATE_QUERY = HERE / "edit_active_dates.graphql"
UPSERT_OP, END_DATE_OP = "UpsertPurchase", "EditChildActiveDates"
TAG_TITLE = "Ad Hoc Only"

TAG_ADDED, TAG_PRESENT, TAG_NOT_FOUND, TAG_FAILED = "tag_added", "tag_present", "tag_not_found", "tag_failed"
END_REMOVED, END_NONE, END_FAILED = "end_date_removed", "end_date_none", "end_date_failed"


class BookingRejected(Exception):
    """Famly refused the booking; nothing was written. `code` is a short code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BookingUnknown(Exception):
    """The booking may or may not have landed. A person looks; it is never sent again."""


@dataclass
class BookingResult:
    purchase_ids: list[str]
    verified: bool                     # what Famly stored matches what was approved
    mismatches: list[str] = field(default_factory=list)


def _doc_text(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#")).lower()


def assert_documents() -> None:
    """The write documents are exactly what they claim: the booking only updates, and nothing here deletes."""
    book, end = _doc_text(UPSERT_QUERY), _doc_text(END_DATE_QUERY)
    if "update(purchases:" not in book or "delete" in book or "mutation upsertpurchase" not in book:
        raise AssertionError("upsert_purchase.graphql is not the booking-only document")
    if "updatechildactivedates(" not in end or "delete" in end or "purchase" in end:
        raise AssertionError("edit_active_dates.graphql is not the end-date-only document")


def _walk_purchases(node, found: list) -> None:
    if isinstance(node, dict):
        if node.get("id") and "sessionBookings" in node:
            found.append(node)
        for value in node.values():
            _walk_purchases(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_purchases(value, found)


def _money(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return None


def check_booking(purchase: dict, body: Any) -> BookingResult:
    """Compare Famly's answer with the approved purchase: one purchase, the same (date, price, pricing group) rows,
    the same discount."""
    found: list[dict] = []
    _walk_purchases((body or {}).get("data"), found)
    if len(found) != 1:
        return BookingResult([str(p["id"]) for p in found], False, ["purchase_count"])
    got = found[0]
    want_rows = sorted((b["purchaseDate"], str(_money(b["bookedPrice"])), b["pricingGroupId"])
                       for b in purchase["sessionBookings"])
    got_rows = sorted((str(b.get("purchaseDate"))[:10], str(_money(b.get("bookedPrice"))), b.get("pricingGroupId"))
                      for b in got.get("sessionBookings") or [])
    mismatches = []
    if want_rows != got_rows:
        mismatches.append("session_rows")
    if _money(got.get("discountAmount")) != _money(purchase["discountAmount"]):
        mismatches.append("discount")
    return BookingResult([str(got["id"])], not mismatches, mismatches)


class FamlyWriter:
    """The three guarded writes. `gql` is the INTERNAL GraphQL client, `rest` the REST client (get and post)."""

    def __init__(self, gql, rest):
        self.gql, self.rest = gql, rest
        assert_documents()

    # -- booking ----------------------------------------------------------------------------------------------
    def book(self, child_id: str, purchase: dict) -> BookingResult:
        if purchase.get("childId") != child_id or purchase.get("productBookings") != [] or not purchase.get("sessionBookings"):
            raise BookingRejected("purchase_not_for_this_child")
        try:
            body = self.gql.execute(query_path=UPSERT_QUERY, operation_name=UPSERT_OP,
                                    variables={"purchases": [purchase]})
        except GraphQLError as exc:
            if (getattr(exc, "body", None) or {}).get("data"):
                raise BookingUnknown() from None          # errors AND data: the mutation may have run
            raise BookingRejected("booking_rejected") from None
        except GraphQLHTTPError as exc:
            if 400 <= int(exc.status_code or 0) < 500:
                raise BookingRejected("booking_rejected") from None
            raise BookingUnknown() from None
        except Exception:  # noqa: BLE001 - a timeout or connection error: the request may have landed
            raise BookingUnknown() from None
        return check_booking(purchase, body)

    # -- administrative writes, settled by reads -----------------------------------------------------------
    def summary(self, child_id: str) -> dict:
        return self.rest.get(f"/v2/children/{child_id}/summary")

    def _tag_ids(self, child_id: str) -> set:
        return {t.get("tagId") for t in (self.summary(child_id).get("tags") or []) if isinstance(t, dict)}

    def tag_id_for(self, institution_id: str) -> str | None:
        """The site's one live "Ad Hoc Only" tag id, or None (none, or more than one: never guessed)."""
        rows = self.rest.get("/v2/tags", params={"institutionId": institution_id})
        live = [t for t in (rows if isinstance(rows, list) else []) if isinstance(t, dict)
                and t.get("title") == TAG_TITLE and not t.get("deletedAt")
                and t.get("institutionId") == institution_id and t.get("tagId")]
        return str(live[0]["tagId"]) if len(live) == 1 else None

    def tag(self, child_id: str, institution_id: str) -> str:
        try:
            tag_id = self.tag_id_for(institution_id)
            if tag_id is None:
                return TAG_NOT_FOUND
            if tag_id in self._tag_ids(child_id):
                return TAG_PRESENT
        except Exception:  # noqa: BLE001
            return TAG_FAILED
        try:
            self.rest.post(f"/v2/tags/{tag_id}/assign", json_body={"childId": child_id})
        except Exception:  # noqa: BLE001 - settled by the read below, never re-sent
            pass
        try:
            return TAG_ADDED if tag_id in self._tag_ids(child_id) else TAG_FAILED
        except Exception:  # noqa: BLE001
            return TAG_FAILED

    def remove_end_date(self, child_id: str) -> str:
        try:
            child = self.summary(child_id)["child"]
            if not child.get("endDate") and not child.get("localEndDate"):
                return END_NONE
            start = str(child.get("localStartDate") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
                return END_FAILED                          # no start date to keep: never guessed
        except Exception:  # noqa: BLE001
            return END_FAILED
        dates = {"startDate": start}
        assert "endInfo" not in dates                      # removal only: the worker never SETS an end date
        try:
            self.gql.execute(query_path=END_DATE_QUERY, operation_name=END_DATE_OP,
                             variables={"childId": child_id, "childActiveDates": dates, "isParent": False})
        except Exception:  # noqa: BLE001 - settled by the read below, never re-sent
            pass
        try:
            after = self.summary(child_id)["child"]
            return END_REMOVED if not after.get("endDate") and not after.get("localEndDate") else END_FAILED
        except Exception:  # noqa: BLE001
            return END_FAILED
