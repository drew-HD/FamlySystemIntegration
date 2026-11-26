"""REGISTRY STAGE 4: the Famly writes that JOIN a nursery deal to a child ad hoc already created
(docs/registry_design.md). Drew's decisions (2026-11-25/26):

  * start date: moved to the nursery start ONLY when that is EARLIER than the child's start in Famly; never later
    (booked ad hoc sessions stay inside the child's active dates);
  * end date: the nursery end date is always set (the deal's end date, or the 31 August after the 4th birthday),
    as onboarding always sends one;
  * the site's "Ad Hoc Only" tag is removed;
  * the room (group) and the booked ad hoc sessions are NEVER changed: nothing here touches them.

Two writes, each sent ONCE and SETTLED BY A READ of the child's summary (never re-sent), like the ad hoc
worker's administrative writes (actions/adhoc/writes.py):

  dates  EditChildActiveDates(childId, {startDate, endInfo: {endDate[, reasonForLeavingId]}}) (internal GraphQL).
         Skipped when the summary already shows both dates.
  tag    POST /v2/tags/<tagId>/unassign {"childId"} (REST). Skipped when the child does not carry the tag.

UNPROVEN LIVE (2026-11-26): the endInfo form of the dates write and the tag unassign path. The adhoc-probe-write steps
`join-dates` and `untag` prove them on a test child first; until then the worker's join must not run live.
Returns short codes only; no error text, no names.
"""

import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
DATES_QUERY = HERE / "edit_active_dates.graphql"
DATES_OP = "EditChildActiveDates"
TAG_TITLE = "Ad Hoc Only"
TAG_UNASSIGN_PATH = "/v2/tags/{tag_id}/unassign"          # to confirm with adhoc-probe-write --step untag

START_MOVED, START_KEPT = "join_start_moved", "join_start_kept"
END_SET, END_KEPT = "join_end_set", "join_end_kept"
DATES_FAILED, END_FAILED = "join_dates_failed", "join_end_date_failed"
SUMMARY_UNREADABLE = "join_summary_unreadable"
TAG_REMOVED, TAG_ABSENT, TAG_NOT_FOUND, TAG_FAILED = ("join_tag_removed", "join_tag_absent", "join_tag_not_found",
                                                      "join_tag_failed")
OK_CODES = (START_MOVED, START_KEPT, END_SET, END_KEPT, TAG_REMOVED, TAG_ABSENT)

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def assert_document() -> None:
    """The dates document only updates active dates: no delete, no purchase, no group."""
    text = "\n".join(line for line in DATES_QUERY.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#")).lower()
    if ("updatechildactivedates(" not in text or "delete" in text or "purchase" in text
            or "group" in text):
        raise AssertionError("edit_active_dates.graphql is not the active-dates-only document")


def _day(value) -> str | None:
    text = str(value or "")[:10]
    return text if _DAY.match(text) else None


def start_to_send(current: str | None, nursery: date) -> str:
    """The start date to keep in Famly: the nursery start only when it is EARLIER (or Famly has none)."""
    wanted = nursery.isoformat()
    return wanted if current is None or wanted < current else current


class JoinWriter:
    """`gql` is the INTERNAL GraphQL client, `rest` the REST client (get and post)."""

    def __init__(self, gql, rest, leaving_reason_id: str | None = None):
        self.gql, self.rest = gql, rest
        self.leaving_reason_id = leaving_reason_id or None
        assert_document()

    def _child(self, child_id: str) -> dict:
        return self.rest.get(f"/v2/children/{child_id}/summary") or {}

    def join(self, child_id: str, nursery_start: date, nursery_end: date, institution_ids: list) -> list[str]:
        """Join the child: the dates, then the tag. Never raises; returns codes."""
        return self.dates(child_id, nursery_start, nursery_end) + [self.untag(child_id, institution_ids)]

    # -- dates ---------------------------------------------------------------------------------------------
    def dates(self, child_id: str, nursery_start: date, nursery_end: date) -> list[str]:
        try:
            child = self._child(child_id).get("child") or {}
            current_start = _day(child.get("localStartDate"))
            current_end = _day(child.get("localEndDate") or child.get("endDate"))
        except Exception:  # noqa: BLE001 - REST error text can carry a body
            return [SUMMARY_UNREADABLE]
        start = start_to_send(current_start, nursery_start)
        end = nursery_end.isoformat()
        assert current_start is None or start <= current_start     # never moves the start LATER
        if end < start:
            return [DATES_FAILED]                                    # never sent: the planner holds for this
        if start == current_start and end == current_end:
            return [START_KEPT, END_KEPT]
        end_info = {"endDate": end}
        if self.leaving_reason_id:
            end_info["reasonForLeavingId"] = self.leaving_reason_id
        try:
            self.gql.execute(query_path=DATES_QUERY, operation_name=DATES_OP,
                             variables={"childId": child_id, "isParent": False,
                                        "childActiveDates": {"startDate": start, "endInfo": end_info}})
        except Exception:  # noqa: BLE001 - settled by the read below, never re-sent
            pass
        try:
            after = self._child(child_id).get("child") or {}
        except Exception:  # noqa: BLE001
            return [DATES_FAILED]
        start_now = _day(after.get("localStartDate"))
        end_now = _day(after.get("localEndDate") or after.get("endDate"))
        if start_now != start:
            return [DATES_FAILED]
        codes = [START_MOVED if start != current_start else START_KEPT]
        codes.append((END_SET if end != current_end else END_KEPT) if end_now == end else END_FAILED)
        return codes

    # -- tag -----------------------------------------------------------------------------------------------
    def _tag_ids(self, child_id: str) -> set:
        return {t.get("tagId") for t in (self._child(child_id).get("tags") or []) if isinstance(t, dict)}

    def tag_id_for(self, institution_id: str) -> str | None:
        """The site's one live "Ad Hoc Only" tag id, or None (none, or more than one: never guessed)."""
        rows = self.rest.get("/v2/tags", params={"institutionId": institution_id})
        live = [t for t in (rows if isinstance(rows, list) else []) if isinstance(t, dict)
                and t.get("title") == TAG_TITLE and not t.get("deletedAt")
                and t.get("institutionId") == institution_id and t.get("tagId")]
        return str(live[0]["tagId"]) if len(live) == 1 else None

    def untag(self, child_id: str, institution_ids: list) -> str:
        """Remove every "Ad Hoc Only" tag (of the given sites) the child carries. `institution_ids`: the child's
        site, or every site when it is not known. A site whose tag cannot be pinned to ONE id is skipped; if no
        site gives a tag at all, `join_tag_not_found` (never guessed)."""
        try:
            tag_ids = [t for t in (self.tag_id_for(i) for i in institution_ids or [] if i) if t]
            if not tag_ids:
                return TAG_NOT_FOUND
            on_child = [t for t in tag_ids if t in self._tag_ids(child_id)]
            if not on_child:
                return TAG_ABSENT
        except Exception:  # noqa: BLE001
            return TAG_FAILED
        for tag_id in on_child:
            try:
                self.rest.post(TAG_UNASSIGN_PATH.format(tag_id=tag_id), json_body={"childId": child_id})
            except Exception:  # noqa: BLE001 - settled by the read below, never re-sent
                pass
        try:
            left = self._tag_ids(child_id)
        except Exception:  # noqa: BLE001
            return TAG_FAILED
        return TAG_FAILED if any(t in left for t in on_child) else TAG_REMOVED
