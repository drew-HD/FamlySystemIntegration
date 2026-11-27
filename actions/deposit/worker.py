"""The deposit worker: one pass over every active job (run from cron every 10 minutes: `deposit-worker`).

  waiting            gather the facts (deal, child, plans, start, bill payer, deposits). Not there yet: keep
                     waiting (held after DEPOSIT_WAIT_DAYS). A gap: HOLD. Else plan and post the Slack approval.
  approved           re-gather and re-plan with today's date (amounts must match what was approved), then add
                     installment 1 and send its invoice (live only; dry: nothing is written).
  scheduled          (live only) each later installment's deposit is added the day before its date; on its date
                     (from 08:00 London) its own invoice is sent unless Famly's regular run covers the child
                     that month (re-checked against the child's start date then).
A write never repeats after an unclear outcome: see writes.py. Logs carry ids and codes only.
"""

import logging
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.hubspot import DEPOSIT_STATUS_ADDED, DEPOSIT_STATUS_NEEDS_ATTENTION

from . import config, famly, messages, planner, reads, store, writes

logger = logging.getLogger(__name__)
LONDON = ZoneInfo("Europe/London")


class Worker:
    def __init__(self, hub, rest, gql, public, notifier, now: datetime | None = None):
        self.hub, self.rest, self.gql, self.public, self.notifier = hub, rest, gql, public, notifier
        self.now = (now or datetime.now(LONDON)).astimezone(LONDON)
        self.today = self.now.date()
        self._stages = None
        self._names: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def stages(self) -> dict:
        if self._stages is None:
            self._stages = self.hub.stage_names()
        return self._stages

    def name(self, job: store.Job, deal: dict | None = None) -> str:
        if deal is not None:
            self._names[job.deal_id] = reads.Facts(deal=deal, stage=("", "")).child_name()
        if job.deal_id not in self._names:
            try:
                self._names[job.deal_id] = reads.Facts(deal=self.hub.get_deposit_deal(job.deal_id),
                                                       stage=("", "")).child_name()
            except Exception:  # noqa: BLE001 - a name is only for the message
                self._names[job.deal_id] = ""
        return self._names[job.deal_id]

    def hold(self, job: store.Job, code: str) -> None:
        store.hold(job, code)
        logger.info("DEPOSIT HELD job=%s code=%s", job.id, code)
        self.notifier.notice(messages.hold_text(self.name(job), job.deal_id, code))
        self.status(job, DEPOSIT_STATUS_NEEDS_ATTENTION)

    def status(self, job: store.Job, value: str) -> None:
        """famly_deposit_status on the deal: live only. HubSpot never blocks Famly."""
        if not config.writes_allowed(job.deal_id):
            return
        try:
            self.hub.write_deposit_status(job.deal_id, value)
        except Exception as exc:  # noqa: BLE001
            logger.error("DEPOSIT status write job=%s failed: %s", job.id, type(exc).__name__)

    def writer(self, job: store.Job) -> famly.DepositWriter:
        return famly.DepositWriter(self.rest, self.gql, writes_allowed=config.writes_allowed(job.deal_id))

    # ------------------------------------------------------------------ the pass
    def run_once(self) -> dict:
        mode = config.mode()
        counts = {"jobs": 0}
        if mode == config.MODE_OFF:
            return counts
        for job in store.jobs((store.WAITING, store.APPROVED, store.SCHEDULED)):
            counts["jobs"] += 1
            try:
                if job.status == store.WAITING:
                    self.plan(job)
                elif job.status == store.APPROVED:
                    self.first(job)
                elif job.status == store.SCHEDULED and mode == config.MODE_LIVE:
                    self.later(job)
            except writes.Held as held:
                self.hold(store.get(job.id), held.code)
            except Exception as exc:  # noqa: BLE001 - try again next pass; never a guess
                logger.error("DEPOSIT job=%s failed: %s", job.id, type(exc).__name__)
        return counts

    def _facts_and_plan(self, job: store.Job):
        facts = reads.gather(self.hub, self.rest, self.public, job.deal_id, self.stages())
        if facts.code:
            return facts, None
        try:
            return facts, planner.plan(facts.deal, self.today, facts.plan_start)
        except planner.Hold as exc:
            facts.code = exc.reason
            return facts, None

    def plan(self, job: store.Job) -> None:
        facts, insts = self._facts_and_plan(job)
        if facts.code in reads.NOT_READY:
            created = datetime.fromisoformat(job.created_at).date()
            if (self.today - created).days > config.wait_days():
                self.hold(job, "waited_too_long")
            return
        if facts.code:
            self.hold(job, facts.code)
            return
        if facts.deposits:
            self.hold(job, "deposit_exists")
            return
        self._save(job, facts, insts)
        nonce = secrets.token_hex(8)
        store.update(job.id, approval_nonce=nonce)
        text = messages.gate_text(facts.child_name(), job.deal_id, facts.deal.get("deposit_preference"), insts,
                                  dry=not config.writes_allowed(job.deal_id))
        ref = self.notifier.gate(text, job.id, nonce)
        if not ref:
            self.hold(store.get(job.id), "slack_post_failed")
            return
        store.transition(job.id, store.WAITING, store.AWAITING_APPROVAL, slack_ref=ref)
        logger.info("DEPOSIT awaiting approval job=%s installments=%s", job.id, len(insts))

    def _save(self, job, facts, insts) -> None:
        store.update(job.id, child_id=facts.child_id, institution_id=facts.institution_id,
                     bill_payer_id=facts.bill_payer_id, preference=str(facts.deal.get("deposit_preference") or ""),
                     total=str(planner.total_of(insts)), plan_start=facts.plan_start.isoformat())
        # Every installment is stored with an invoice date (its own date): whether Famly's regular run covers a
        # later one is decided again ON that date, against the child's start date then.
        store.save_installments(job.id, [{"number": i.number, "amount": i.amount, "on_date": i.on.isoformat(),
                                          "invoice_on": (i.invoice_on or i.on).isoformat()} for i in insts])

    def first(self, job: store.Job) -> None:
        name_deal = None
        saved = store.installments(job.id)
        first = saved[0] if saved else None
        untouched = first is not None and first.deposit_state == store.D_PENDING and first.invoice_state == store.I_DUE
        if not config.writes_allowed(job.deal_id):
            if config.mode() == config.MODE_DRY and untouched:
                store.transition(job.id, store.APPROVED, store.DRY_DONE)
                self.notifier.notice(messages.done_text(self.name(job), job.deal_id,
                                                        "approved in dry run; nothing was written."))
            else:                                       # not live, and part of it was already written
                self.hold(job, "not_live")
            return
        if first is None:
            self.hold(job, "plan_changed")
            return
        if untouched:
            facts, insts = self._facts_and_plan(job)
            name_deal = facts.deal
            if facts.code:
                self.hold(job, facts.code)
                return
            if [i.amount for i in insts] != [i.amount for i in saved]:
                self.hold(job, "plan_changed")
                return
            if (facts.child_id, facts.institution_id, facts.bill_payer_id) != \
                    (job.child_id, job.institution_id, job.bill_payer_id):
                self.hold(job, "famly_ids_changed")
                return
            if facts.deposits:
                self.hold(job, "deposit_exists")
                return
            # The later installments keep the dates shown in Slack; the second must still be more than 14 days
            # away, else the approval is out of date (cancel and enqueue again).
            if len(saved) > 1 and (date.fromisoformat(saved[1].on_date) - self.today).days <= planner.MIN_DAYS_TO_SECOND:
                self.hold(job, "plan_changed")
                return
            if not store.redate_first(job.id, self.today.isoformat()):   # "added on approval": dated today
                self.hold(job, "plan_changed")
                return
        job = store.get(job.id)
        first = store.installments(job.id)[0]
        writer = self.writer(job)
        writes.add_deposit(writer, self.rest, job, first)
        writes.send_invoice(writer, self.gql, job, store.installments(job.id)[0], self.today)
        more = len(store.installments(job.id)) > 1
        store.transition(job.id, store.APPROVED, store.SCHEDULED if more else store.DONE)
        self.status(job, DEPOSIT_STATUS_ADDED)
        self.notifier.notice(messages.done_text(self.name(job, name_deal), job.deal_id,
                                                f"{messages.money(first.amount)} added and invoiced."))

    def later(self, job: store.Job) -> None:
        for inst in store.installments(job.id)[1:]:
            on = date.fromisoformat(inst.on_date)
            if inst.deposit_state != store.D_ADDED and self.today >= on - timedelta(days=1):
                self._same_payer(job)
                writes.add_deposit(self.writer(job), self.rest, job, inst)
                self.notifier.notice(messages.done_text(self.name(job), job.deal_id,
                                                        f"installment {inst.number} ({messages.money(inst.amount)},"
                                                        f" dated {messages.fmt_date(on)}) added."))
                inst = [i for i in store.installments(job.id) if i.number == inst.number][0]
            if (inst.invoice_state == store.I_DUE and inst.deposit_state == store.D_ADDED
                    and self.today >= date.fromisoformat(inst.invoice_on)
                    and self.now.hour >= config.INVOICE_SEND_HOUR_LONDON):
                start = reads.child_start(self.rest.get(f"/v2/children/{job.child_id}/summary"))
                if start is None:
                    raise writes.Held("start_date_missing")
                if planner.regular_run_covers(on, start):
                    store.inst_transition(job.id, inst.number, "invoice_state", store.I_DUE, store.I_COVERED)
                    continue
                self._same_payer(job)
                writes.send_invoice(self.writer(job), self.gql, job, inst, self.today)
                self.notifier.notice(messages.done_text(self.name(job), job.deal_id,
                                                        f"installment {inst.number} invoiced."))
        left = [i for i in store.installments(job.id)
                if i.deposit_state != store.D_ADDED or i.invoice_state not in (store.I_SENT, store.I_COVERED)]
        if not left:
            store.transition(job.id, store.SCHEDULED, store.DONE)
            logger.info("DEPOSIT done job=%s", job.id)

    def _same_payer(self, job: store.Job) -> None:
        payer, code = reads.the_bill_payer(self.public, job.institution_id, job.child_id)
        if code or payer != job.bill_payer_id:
            raise writes.Held(code or "bill_payer_changed")
