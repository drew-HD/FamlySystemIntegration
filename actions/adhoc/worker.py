"""The ad hoc worker, steps 2 and 3a: the Slack gate, and (live mode) the writes for an EXISTING child. In dry mode it
is READ-ONLY: it never writes to Famly or HubSpot. Live mode books, tags and removes the end date only after a Slack
Approve, and only when the re-plan hash equals the approved one. A plan that needs a NEW child (step 3b) creates
the child, its contacts and the bill payer first (actions/adhoc/new_child.py), then books and tags.

For each queued deal it runs the planner (actions/adhoc_plan) and then:

  * HOLDS (anything that could mis-book or misprice): a plain-English Slack notice with a Retry button; the job is
    `held`. The one exception is a lone `parent_email_matches_a_different_child` with exactly one candidate: the
    job posts the CHILD-MATCH question (the Famly child's name and the deal id) and waits `awaiting_approval`.
    "That's the child" or "Create a new child" is recorded, the job is re-queued, and the worker plans again with
    that answer.
  * a clean plan: the PLAN APPROVAL message (Approve / Reject); the job waits `awaiting_approval`. Approve
    re-queues it; the worker re-plans and, if the plan hash is unchanged, finishes `dry_done` (live mode will
    write here, in a later step). A plan that changed after approval is asked again once, then `needs_review`.

Anyone in the channel may click (see approval.py).

A job from the WEBHOOK must be an Approved deal in the Ad Hoc pipeline, else it is held `stage_not_eligible` (a CLI
job may be any stage; the plan shows the flag `stage_not_approved`). Returns codes and ids only; a worker error is
logged by TYPE only.
"""

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from actions.adhoc_plan import planner

from . import config, messages, new_child, store, writes

logger = logging.getLogger(__name__)

POLL_SECONDS = 10.0
STAGE_NOT_ELIGIBLE = "stage_not_eligible"
MATCH_HOLD = "parent_email_matches_a_different_child"
PLAN_UNSTABLE = "plan_unstable"


class _Silent:
    """A notifier that posts nothing (for tests that do not care about Slack)."""

    def gate(self, *a, **k): return "silent:1"
    def match(self, *a, **k): return "silent:1"
    def hold(self, *a, **k): return "silent:1"
    def notice(self, *a, **k): return True


@dataclass
class Deps:
    # deal id (+ the person's match answer) -> plan, or {"error": code}
    plan_deal: Callable[..., dict]
    child_name: Callable[[str], str | None] = lambda child_id: None     # Famly name, memory only, for the match question
    notifier: Any = None
    writes: Any = None            # actions.adhoc.writes.FamlyWriter, live mode only
    # Step 3b (a NEW child), live mode only:
    public: Any = None            # the public-API GraphQL client (child and contact creates, bill payer stage)
    famly_factory: Callable[[], Any] | None = None       # a FRESH FamlyReads per call (it memoises its roster)
    deal_context: Callable[[str], tuple] | None = None   # deal id -> (deal properties, contacts), in memory only
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self):
        if self.notifier is None:
            self.notifier = _Silent()


def default_deps() -> Deps:
    from actions.adhoc_plan.readers import Readers

    from .notifier import SlackNotifier
    readers = Readers()
    famly_writer, public, factory, context = None, None, None, None
    if config.mode() == config.MODE_LIVE:                  # the write clients exist only in live mode
        from actions.onboard_dry_run.famly_reads import FamlyReads
        from actions.urn_lookup import runner as urn_runner
        from core.client import GraphQLClient
        from core.rest_client import RestClient
        famly_writer = writes.FamlyWriter(GraphQLClient(), RestClient())
        public = urn_runner._public_client()
        factory = lambda: FamlyReads(public)  # noqa: E731
        context = readers.deal_and_contacts
    return Deps(plan_deal=readers.plan_deal, child_name=readers.child_name, notifier=SlackNotifier(),
                writes=famly_writer, public=public, famly_factory=factory, deal_context=context)


def recover() -> int:
    return store.recover_in_flight()


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]


def run_once(deps: Deps | None = None) -> dict:
    """Run at most one queued job. Returns codes and ids only."""
    run_mode = config.mode()
    if run_mode == config.MODE_OFF:
        return {"ran": False, "mode": run_mode}
    job = store.claim_next()
    if job is None:
        return {"ran": False, "mode": run_mode}

    deps = deps or default_deps()
    try:
        _process(job, deps, run_mode)
    except Exception as exc:  # noqa: BLE001 - a job must never die silently
        logger.error("adhoc: job %s hit %s", job.id, type(exc).__name__)   # type only: text could hold data
        _fail_job(job, deps)
    final = store.get(job.id)
    return {"ran": True, "mode": run_mode, "jobId": job.id, "dealId": job.deal_id, "status": final.status,
            "code": final.last_error_code, "holds": final.holds, "flags": final.flags}


class _Stop:
    """SIGTERM / SIGINT set this; the loop checks it only between jobs, so a job is never cut off mid-way."""

    def __init__(self):
        self.requested = False

    def __call__(self) -> bool:
        return self.requested

    def request(self, *_args) -> None:
        self.requested = True


ERROR_BACKOFF_SECONDS = 5.0


def _fail_job(job: store.Job, deps: Deps) -> None:
    """After an exception: if the booking request was already SENT the job is `needs_review` (a person must check
    Famly; Retry must not look safe), else `failed`. Nothing here may raise out of the loop."""
    sent = False
    try:
        row = store.get(job.id)
        sent = bool(row.write_sent or row.child_create_sent)       # a sent create also needs a person to look
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc: job %s re-read hit %s", job.id, type(exc).__name__)
        sent = True                    # cannot tell: treat as possibly booked, the safe side
    try:
        if sent:
            store.set_status(job.id, store.NEEDS_REVIEW, last_error_code="worker_error_after_write")
        else:
            store.set_status(job.id, store.FAILED, last_error_code="worker_error")
    except Exception as exc:  # noqa: BLE001
        logger.error("adhoc: job %s status update hit %s", job.id, type(exc).__name__)
        return
    if sent:
        try:
            deps.notifier.notice(f"⚠️ Ad hoc deal {messages.deal_link(job.deal_id)}: "
                                 f"{messages.SENTENCES['worker_error_after_write']}")
        except Exception as exc:  # noqa: BLE001
            logger.error("adhoc: job %s notice hit %s", job.id, type(exc).__name__)


def run_forever(poll: float = POLL_SECONDS, deps: Deps | None = None, *, should_stop: Callable[[], bool] | None = None,
                sleep: Callable[[float], None] = time.sleep, install_signals: bool | None = None) -> None:
    """The service loop. Recovers once, then: a job ran -> loop again at once (drain); idle -> sleep `poll`; an
    exception in an iteration -> log its TYPE only, back off, continue. SIGTERM/SIGINT finish the current job and
    return. The mode is read fresh each iteration (off = idle but still polling)."""
    stop = _Stop()
    if should_stop is None:
        should_stop = stop
        install_signals = True if install_signals is None else install_signals
    if install_signals:
        import signal
        signal.signal(signal.SIGTERM, stop.request)
        signal.signal(signal.SIGINT, stop.request)
    logger.info("adhoc worker started: mode=%s store=%s", config.mode(), store.store_path().name)
    recovered = False
    last_mode = None
    while not should_stop():
        try:
            if not recovered:
                try:
                    recover()
                    recovered = True
                except Exception as exc:  # noqa: BLE001 - retried on each later iteration
                    logger.error("adhoc: recover hit %s", type(exc).__name__)
                    sleep(ERROR_BACKOFF_SECONDS)       # no job runs until recovery has succeeded
                    continue
            current = config.mode()
            if current != last_mode:
                if current == config.MODE_OFF:
                    logger.info("adhoc worker idle: ADHOC_MODE is off (still polling)")
                elif last_mode is not None:
                    logger.info("adhoc worker mode is now %s", current)
                last_mode = current
            if run_once(deps)["ran"]:
                continue
            sleep(poll)
        except Exception as exc:  # noqa: BLE001 - never exit on an iteration error; type only
            logger.error("adhoc: loop iteration hit %s", type(exc).__name__)
            sleep(ERROR_BACKOFF_SECONDS)
    logger.info("adhoc worker stopped cleanly")


def _process(job: store.Job, deps: Deps, run_mode: str) -> None:
    store.update(job.id, mode=run_mode)
    confirmed = job.candidate_child_id if job.match_decision == "confirmed" else None
    plan = deps.plan_deal(job.deal_id, confirmed_child_id=confirmed, force_new=job.match_decision == "new")
    if "error" in plan:
        code = plan["error"]
        if code == "deal_not_found":
            store.set_status(job.id, store.FAILED, last_error_code=code, holds=[code], flags=[])
            deps.notifier.notice(messages.hold_text(job.deal_id, [code], run_mode))
        else:
            _hold(job, deps, [code], run_mode)
        return

    holds = list(plan["holds"])
    if job.source == "webhook" and planner.GO_STAGE != _norm(plan.get("stage")):
        holds.append(STAGE_NOT_ELIGIBLE)
    holds = sorted(set(holds))
    flags = [f for f in plan["flags"] if not (job.source == "webhook" and f == "stage_not_approved")]
    digest = plan_hash(plan)
    child_ids = plan["match"]["childIds"]
    store.update(job.id, plan_hash=digest, plan_json=plan, holds=holds, flags=flags,
                 child_id=child_ids[0] if len(child_ids) == 1 else None, last_error_code=None)

    if holds:
        candidates = plan["match"].get("candidateIds") or []
        if holds == [MATCH_HOLD] and len(candidates) == 1 and job.match_decision is None:
            _ask_match(job, deps, plan, digest, candidates[0], run_mode)
        else:
            _hold(job, deps, holds, run_mode, plan=plan, flags=flags, digest=digest)
        return

    if job.approved_hash == digest:
        # Approved in Slack and unchanged on the re-check. Live mode writes; dry mode writes nothing.
        if run_mode == config.MODE_LIVE:
            _write_phase(job, deps, plan, digest)
            return
        store.set_status(job.id, store.DRY_DONE, last_error_code=None)
        deps.notifier.notice(f"✅ {messages.DRY}Ad hoc approved for deal {messages.deal_link(job.deal_id)}. "
                             "Nothing was written (dry run).")
        return
    changed = job.approved_hash is not None
    if changed and job.reask_count >= 1:
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code=PLAN_UNSTABLE)
        deps.notifier.notice(f"⚠️ Ad hoc deal {messages.deal_link(job.deal_id)}: the plan keeps changing after "
                             "approval, so it needs a person to look at it.")
        return
    _ask_plan(job, deps, plan, digest, run_mode, changed)


def _end(job, deps, status: str, code: str, plan: dict | None = None, problems: list | None = None,
         extra: list | None = None) -> None:
    store.set_status(job.id, status, last_error_code=",".join([code, *(extra or [])]))
    text = (messages.done_text(plan, [], {}, problems or [code], "live") if plan
            else f"⚠️ Ad hoc deal {messages.deal_link(job.deal_id)}: {messages.SENTENCES.get(code, code)}")
    if extra:                                           # codes of earlier problems (ids and codes only, no names)
        text += "\n" + "\n".join(f"• {messages.SENTENCES.get(c.split(':')[0], c)}" for c in extra) \
            + f"\nCodes: {', '.join(extra)}"
    deps.notifier.notice(text)


def _write_phase(job, deps, plan, digest: str) -> None:
    """LIVE: write the approved plan. An EXISTING child is booked and tagged and loses its end date; a NEW child is
    created first (step 3b). Book first (the money step), then the administrative writes. Every request is sent once
    and never retried; an unclear outcome ends `needs_review`."""
    if deps.writes is None:
        raise RuntimeError("no Famly writer configured")
    store.set_status(job.id, store.WRITING, plan_hash=digest)
    problems: list[str] = []
    outcomes: list[str] = []
    if plan["match"]["childIds"]:
        child_id = plan["match"]["childIds"][0]
        if _contacts_missing(job, child_id):
            problems.append("contacts_missing")        # a Retry found a child an earlier job created
    else:
        child_id = _create_new_child(job, deps, plan, outcomes, problems)
        if child_id is None:
            return
    purchase = {**plan["purchase"], "childId": child_id}   # the plan hash has childId None; filled AFTER creation
    new = not plan["match"]["childIds"]
    if not store.mark_write_sent(job.id):          # persisted BEFORE the request: a job never books twice
        _end(job, deps, store.NEEDS_REVIEW, "duplicate_write_blocked", extra=problems)
        return
    try:
        result = deps.writes.book(child_id, purchase)
    except writes.BookingRejected as exc:
        _end(job, deps, store.FAILED, exc.code, extra=problems)
        return
    except writes.BookingUnknown:
        _end(job, deps, store.NEEDS_REVIEW, "booking_outcome_unknown", extra=problems)
        return
    outcomes.append(f"booked:{len(purchase['sessionBookings'])}")
    store.update(job.id, outcomes=outcomes)
    if not result.verified:
        problems.append("booking_mismatch:" + "+".join(result.mismatches))
    results: dict[str, str] = {}
    if plan["adHocOnly"]:
        results["tag"] = deps.writes.tag(child_id, plan["siteId"])
        outcomes.append(results["tag"])
        if new:
            results["end"] = writes.END_NONE            # a new child has no end date: that step is skipped
        else:
            results["end"] = deps.writes.remove_end_date(child_id)
            outcomes.append(results["end"])
        store.update(job.id, outcomes=outcomes)
        if results["tag"] not in (writes.TAG_ADDED, writes.TAG_PRESENT):
            problems.append(results["tag"])
        if results["end"] not in (writes.END_REMOVED, writes.END_NONE):
            problems.append(results["end"])
    store.set_status(job.id, store.NEEDS_REVIEW if problems else store.DONE,
                     last_error_code=",".join(problems) or None)
    deps.notifier.notice(messages.done_text(plan, result.purchase_ids, results, problems, "live",
                                            child_id=child_id if new else None))


def _contacts_missing(job, child_id: str) -> bool:
    """A child that an EARLIER job of this deal created (a Retry found it by the deal reference): its contacts are not
    created again; if that job did not record every contact as created or present, a person is told."""
    for earlier in store.earlier_creates_for_deal(job.deal_id, child_id, job.id):
        entries = [o for o in earlier.outcomes or [] if str(o).startswith("contact:")]
        if not entries or any(o.rsplit(":", 1)[1] not in new_child.OK_OUTCOMES for o in entries):
            return True
    return False


def _create_new_child(job, deps, plan, outcomes: list, problems: list) -> str | None:
    """Create the child, its contacts, the bill payer and the recipient link. Returns the child id, or None when the
    job has ended (nothing further is written). Contact, bill payer and recipient problems are collected in
    `problems` and never stop the booking."""
    if deps.public is None or deps.famly_factory is None or deps.deal_context is None:
        raise RuntimeError("no Famly create client configured")
    deal, contacts = deps.deal_context(job.deal_id)       # names and emails: memory only, for this call
    planned = {(str(e["hubspotContactId"]), bool(e["isBillPayer"])) for e in plan.get("contacts") or []}
    if (deal is None or contacts is None or not plan.get("contacts")
            or planned != {(str(c.hubspot_id), bool(c.is_bill_payer)) for c in contacts}):
        _end(job, deps, store.FAILED if deal is None or contacts is None else store.NEEDS_REVIEW,
             "deal_not_readable" if deal is None or contacts is None else "plan_changed")
        return None                                         # nothing was sent
    famly = deps.famly_factory()
    try:
        child_id = new_child.ensure_child(job, deps.public, famly, plan, deal, deps.sleep)
    except new_child.StopJob as stop:
        _end(job, deps, stop.status, stop.code)
        return None
    made = new_child.create_contacts(job, deps.public, famly, plan, contacts, child_id, deps.sleep)
    outcomes += made.outcomes
    problems += made.problems
    store.update(job.id, outcomes=outcomes)
    payer = None
    if plan.get("billPayer"):
        payer = new_child.bill_payer_stage(job, deps.public, famly, plan, contacts, child_id, deps.sleep)
        outcomes.append(f"billpayer:{payer.code}")
        store.update(job.id, outcomes=outcomes)
        if payer.code not in new_child.payer_scan.SUCCESS_CODES:
            problems.append(payer.code)
        link_code, link_problem = new_child.recipient_stage(
            deps.public, famly, plan, contacts, made.contact_ids, payer, deps.sleep)
        if link_code:
            outcomes.append(f"billpayerlink:{link_code}")
            store.update(job.id, outcomes=outcomes)
            if link_problem:
                problems.append(link_code)
    return child_id


def _nonce() -> str:
    return uuid.uuid4().hex


def _ask_plan(job, deps, plan, digest, run_mode, changed: bool) -> None:
    nonce = _nonce()
    store.set_status(job.id, store.AWAITING_APPROVAL, approval_nonce=nonce, question="plan", approval_ref=None,
                     reask_count=job.reask_count + (1 if changed else 0))
    ids = plan["match"]["childIds"]
    name = deps.child_name(ids[0]) if len(ids) == 1 else None            # memory only, for the message
    ref = deps.notifier.gate(messages.gate_text(plan, digest, run_mode, changed, child_name=name), job.id, digest, nonce)
    _record_post(job, ref)


def _ask_match(job, deps, plan, digest, candidate: str, run_mode: str) -> None:
    nonce = _nonce()
    store.set_status(job.id, store.AWAITING_APPROVAL, approval_nonce=nonce, question="match",
                     candidate_child_id=candidate, approval_ref=None)
    text = messages.match_text(plan, deps.child_name(candidate), candidate, run_mode)   # the name is not stored
    _record_post(job, deps.notifier.match(text, job.id, digest, nonce))


def _hold(job, deps, codes, run_mode, plan=None, flags=None, digest=None) -> None:
    nonce = _nonce()
    store.set_status(job.id, store.HELD, holds=list(codes), flags=flags or [], approval_nonce=nonce,
                     last_error_code=codes[0] if codes else None)
    ref = deps.notifier.hold(messages.hold_text(job.deal_id, codes, run_mode, (plan or {}).get("details")), job.id, nonce)
    if ref:
        store.update(job.id, approval_ref=ref)


def _record_post(job, ref: str | None) -> None:
    """Keep the Slack message ref; if Slack refused the post, nobody can answer, so hold the job."""
    if ref:
        store.update(job.id, approval_ref=ref)
    else:
        store.set_status(job.id, store.HELD, last_error_code="slack_post_failed", approval_nonce=None)


def _norm(stage) -> str:
    return " ".join(str(stage or "").split()).casefold()
