"""The ONLY HubSpot writes the ad hoc worker makes: the child's URN and the Famly child id onto the DEAL (Drew,
2026-11-25: so the deal records which child it made and a Retry or a person can find it), plus (registry stage 3) a
NOTE on a deal and the `famly_followup` dropdown on an OPERATIONS deal, so the CS team sees what needs checking
(`add_note` / `set_followup`: ids and codes only; a failure is a code, never a stop). For the URN and child id, the
same rules as onboarding's `_write_allocated_urn` and `Syncer`:

  * only `child_urn` and `famly_childid`, nothing else (a guard, not a convention);
  * only when the property is EMPTY, or already holds the same value; a DIFFERENT value is never overwritten;
  * every write is CONFIRMED by re-reading the deal; a request that failed but whose value is on the deal anyway
    (a 5xx after it ran) counts as written;
  * the URN is written BEFORE any Famly create and a failure stops the job (nothing sent to Famly), exactly as
    onboarding does; the child id is written after the child is confirmed and a failure is only a problem code
    (HubSpot never blocks Famly).
Values are ids and codes; nothing personal passes through here."""

import logging

from core import hubspot as hs
from core.hubspot import HubSpotError

logger = logging.getLogger(__name__)

ALLOWED = frozenset({hs.CHILD_URN_PROPERTY, hs.FAMLY_CHILDID_PROPERTY})
URN_WRITE_FAILED = "urn_hubspot_write_failed"
CHILDID_WRITTEN, CHILDID_PRESENT, CHILDID_WRITE_FAILED = "childid_written", "childid_present", "hubspot_childid_write_failed"


class WritebackFailed(Exception):
    """The deal could not be given the value; `code` is a short code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DealWriter:
    """`client` is a core.hubspot.HubSpotClient (read and write)."""

    def __init__(self, client):
        # Fail at start-up, not mid-job: the plain HubSpotClient has no write_deal_properties (HubSpotDealWriter does).
        if not (callable(getattr(client, "get_deal", None)) and callable(getattr(client, "write_deal_properties", None))):
            raise TypeError("DealWriter needs a client with get_deal and write_deal_properties (HubSpotDealWriter)")
        self.client = client

    def _read(self, deal_id: str, prop: str) -> str:
        return str(self.client.get_deal(str(deal_id)).get(prop) or "").strip()

    def _ensure(self, deal_id: str, prop: str, value: str, same) -> str:
        """'present' | 'written'. Raises WritebackFailed('different' | 'unconfirmed') or HubSpotError/ValueError."""
        assert prop in ALLOWED, prop
        current = self._read(deal_id, prop)
        if current:
            if same(current, value):
                return "present"
            raise WritebackFailed("different")                 # never overwrite another value
        try:
            self.client.write_deal_properties(str(deal_id), {prop: value})
        except (HubSpotError, ValueError):
            if same(self._read(deal_id, prop), value):           # the request failed but the value IS there
                return "written"
            raise
        if not same(self._read(deal_id, prop), value):
            raise WritebackFailed("unconfirmed")
        return "written"

    def ensure_urn(self, deal_id: str, urn: str) -> str:
        """Put the allocated URN on the deal before the child is created. Raises WritebackFailed(URN_WRITE_FAILED)."""
        if not urn:
            raise WritebackFailed(URN_WRITE_FAILED)
        try:
            return self._ensure(deal_id, hs.CHILD_URN_PROPERTY, urn, lambda a, b: a.upper() == b.upper())
        except (WritebackFailed, HubSpotError, ValueError) as exc:
            logger.error("adhoc: urn write to HubSpot failed (%s)", type(exc).__name__)    # type only
            raise WritebackFailed(URN_WRITE_FAILED) from None

    def add_note(self, deal_id: str, body: str) -> str:
        """A note on the deal (ids and codes only). Never raises: 'note_added' | 'note_failed'."""
        create = getattr(self.client, "create_deal_note", None)
        try:
            if not callable(create):
                raise ValueError("no note writer")
            create(str(deal_id), body)
        except (HubSpotError, ValueError) as exc:
            logger.error("adhoc: HubSpot note failed (%s)", type(exc).__name__)
            return "note_failed"
        return "note_added"

    def set_followup(self, deal_id: str, value: str) -> str:
        """The CS follow-up dropdown on an operations deal. Never raises: 'followup_written' | 'followup_failed'
        (e.g. the property has not been created in HubSpot yet)."""
        write = getattr(self.client, "write_followup", None)
        try:
            if not callable(write):
                raise ValueError("no follow-up writer")
            write(str(deal_id), value)
        except (HubSpotError, ValueError) as exc:
            logger.error("adhoc: HubSpot follow-up write failed (%s)", type(exc).__name__)
            return "followup_failed"
        return "followup_written"

    def ensure_child_id(self, deal_id: str, child_id: str) -> str:
        """Put the Famly child id on the deal after the child is confirmed. Never raises: returns a code."""
        try:
            result = self._ensure(deal_id, hs.FAMLY_CHILDID_PROPERTY, child_id, lambda a, b: a == b)
        except (WritebackFailed, HubSpotError, ValueError) as exc:
            logger.error("adhoc: child id write to HubSpot failed (%s)", type(exc).__name__)
            return CHILDID_WRITE_FAILED
        return CHILDID_WRITTEN if result == "written" else CHILDID_PRESENT
