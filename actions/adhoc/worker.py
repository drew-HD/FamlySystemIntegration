"""The ad hoc worker, step 1: DRY MODE ONLY. READ-ONLY: it never writes to Famly or HubSpot.

For each queued deal it runs the planner (actions/adhoc_plan) and records the plan, its holds and its flags on
the job: `dry_done` when there are no holds, `held` when there are. Later steps add the Slack gate and the
writes; `live` mode is refused here until then.

A job from the WEBHOOK must be an Approved deal in the Ad Hoc pipeline, else it is held `stage_not_eligible`
(a CLI job may be any stage; the plan shows `stage_not_approved` as a flag). Returns codes and ids only;
a worker error is logged by TYPE only.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from actions.adhoc_plan import planner

from . import config, store

logger = logging.getLogger(__name__)

POLL_SECONDS = 30.0
STAGE_NOT_ELIGIBLE = "stage_not_eligible"


@dataclass
class Deps:
    plan_deal: Callable[[str], dict]          # deal id -> plan, or {"error": code}


def default_deps() -> Deps:
    from actions.adhoc_plan.readers import Readers
    readers = Readers()
    return Deps(plan_deal=readers.plan_deal)


def recover() -> int:
    return store.recover_in_flight()


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]


def run_once(deps: Deps | None = None) -> dict:
    """Run at most one queued job. Returns codes and ids only."""
    run_mode = config.mode()
    if run_mode == config.MODE_OFF:
        return {"ran": False, "mode": run_mode}
    if run_mode == config.MODE_LIVE:
        logger.error("adhoc: ADHOC_MODE=live is not built yet; the worker does nothing")
        return {"ran": False, "mode": run_mode, "refused": "live_not_built"}
    job = store.claim_next()
    if job is None:
        return {"ran": False, "mode": run_mode}

    deps = deps or default_deps()
    try:
        _process(job, deps, run_mode)
    except Exception as exc:  # noqa: BLE001 - a job must never die silently
        logger.error("adhoc: job %s hit %s", job.id, type(exc).__name__)   # type only: text could hold data
        store.set_status(job.id, store.FAILED, last_error_code="worker_error")
    final = store.get(job.id)
    return {"ran": True, "mode": run_mode, "jobId": job.id, "dealId": job.deal_id, "status": final.status,
            "code": final.last_error_code, "holds": final.holds, "flags": final.flags}


def run_forever(poll: float = POLL_SECONDS, deps: Deps | None = None) -> None:
    recover()
    while True:
        if not run_once(deps)["ran"]:
            time.sleep(poll)


def _process(job: store.Job, deps: Deps, run_mode: str) -> None:
    store.update(job.id, mode=run_mode)
    plan = deps.plan_deal(job.deal_id)
    if "error" in plan:
        code = plan["error"]
        store.set_status(job.id, store.FAILED if code == "deal_not_found" else store.HELD,
                         last_error_code=code, holds=[code], flags=[])
        return
    holds = list(plan["holds"])
    if job.source == "webhook" and planner.GO_STAGE != _norm(plan.get("stage")):
        holds.append(STAGE_NOT_ELIGIBLE)
    flags = [f for f in plan["flags"] if not (job.source == "webhook" and f == "stage_not_approved")]
    child_ids = plan["match"]["childIds"]
    store.set_status(
        job.id, store.HELD if holds else store.DRY_DONE, plan_hash=plan_hash(plan), plan_json=plan,
        holds=sorted(set(holds)), flags=flags, child_id=child_ids[0] if len(child_ids) == 1 else None,
        last_error_code=None)


def _norm(stage) -> str:
    return " ".join(str(stage or "").split()).casefold()
