"""The ad hoc worker, steps 2 and 3a: the Slack gate, and (live mode) the writes for an EXISTING child. In dry mode it
is READ-ONLY: it never writes to Famly or HubSpot. Live mode books, tags and removes the end date only after a Slack
Approve, and only when the re-plan hash equals the approved one; a plan that needs a NEW child is held
(`new_child_not_built`) until step 3b.

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

from . import config, messages, store, writes

logger = logging.getLogger(__name__)

POLL_SECONDS = 10.0
STAGE_NOT_ELIGIBLE = "stage_not_eligible"
MATCH_HOLD = "parent_email_matches_a_different_child"
PLAN_UNSTABLE = "plan_unstable"
NEW_CHILD_NOT_BUILT = "new_child_not_built"          # until step 3b, live mode cannot create a child


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

    def __post_init__(self):
        if self.notifier is None:
            self.notifier = _Silent()


def default_deps() -> Deps:
    from actions.adhoc_plan.readers import Readers

    from .notifier import SlackNotifier
    readers = Readers()
    famly_writer = None
    if config.mode() == config.MODE_LIVE:                  # the write clients exist only in live mode
        from core.client import GraphQLClient
        from core.rest_client import RestClient
        famly_writer = writes.FamlyWriter(GraphQLClient(), RestClient())
    return Deps(plan_deal=readers.plan_deal, child_name=readers.child_name, notifier=SlackNotifier(),
                writes=famly_writer)


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
        sent = bool(store.get(job.id).write_sent)
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
    if (run_mode == config.MODE_LIVE and not plan["match"]["childIds"] and MATCH_HOLD not in holds
            and "purchase" in plan):
        holds.append(NEW_CHILD_NOT_BUILT)          # step 3a writes for an existing child only
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


def _end(job, deps, status: str, code: str, plan: dict | None = None, problems: list | None = None) -> None:
    store.set_status(job.id, status, last_error_code=code)
    deps.notifier.notice(messages.done_text(plan, [], {}, problems or [code], "live") if plan
                         else f"⚠️ Ad hoc deal {messages.deal_link(job.deal_id)}: "
                              f"{messages.SENTENCES.get(code, code)}")


def _write_phase(job, deps, plan: dict, digest: str) -> None:
    """LIVE: write the approved plan for an EXISTING child. Book first (the money step), then the administrative
    tag and end date. Every request is sent once and never retried; an unclear outcome ends `needs_review`."""
    if deps.writes is None:
        raise RuntimeError("no Famly writer configured")
    child_id = plan["match"]["childIds"][0]
    purchase = plan["purchase"]
    store.set_status(job.id, store.WRITING, plan_hash=digest)
    if not store.mark_write_sent(job.id):          # persisted BEFORE the request: a job never books twice
        _end(job, deps, store.NEEDS_REVIEW, "duplicate_write_blocked")
        return
    outcomes: list[str] = []
    try:
        result = deps.writes.book(child_id, purchase)
    except writes.BookingRejected as exc:
        _end(job, deps, store.FAILED, exc.code)
        return
    except writes.BookingUnknown:
        _end(job, deps, store.NEEDS_REVIEW, "booking_outcome_unknown")
        return
    outcomes.append(f"booked:{len(purchase['sessionBookings'])}")
    store.update(job.id, outcomes=outcomes)
    problems: list[str] = []
    if not result.verified:
        problems.append("booking_mismatch:" + "+".join(result.mismatches))
    results: dict[str, str] = {}
    if plan["adHocOnly"]:
        results["tag"] = deps.writes.tag(child_id, plan["siteId"])
        results["end"] = deps.writes.remove_end_date(child_id)
        outcomes += [results["tag"], results["end"]]
        store.update(job.id, outcomes=outcomes)
        if results["tag"] not in (writes.TAG_ADDED, writes.TAG_PRESENT):
            problems.append(results["tag"])
        if results["end"] not in (writes.END_REMOVED, writes.END_NONE):
            problems.append(results["end"])
    store.set_status(job.id, store.NEEDS_REVIEW if problems else store.DONE,
                     last_error_code=",".join(problems) or None)
    deps.notifier.notice(messages.done_text(plan, result.purchase_ids, results, problems, "live"))


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
