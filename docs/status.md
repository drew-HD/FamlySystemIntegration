# Status (current state, not a log)

Full history: docs/archive/status_to_2026-11-18.md. Design docs in docs/: famly_onboarding_pipeline_design, onboarding_bill_payer_design, onboarding_pipeline_state_and_checks, test_suite_report, handovers, reports_2026-10-27.
Add new entries as one or two lines. When this file passes 8 KB, move the oldest detail into the archive.

## Live / built (as of 2026-10-29 entries; confirm what is deployed under "To confirm")
- Onboarding pipeline (`actions/onboard`): HubSpot deal -> queue -> Slack Gate A approval -> Famly child, contacts and bill payer CREATES (never updates or deletes), then write-back to HubSpot. Webhook `POST /intake` action `onboard_enqueue`. End date always sent; HubSpot never blocks Famly; Retry button on hold notices.
- Plan write: `plan_write` (preview, Slack Approve commits via `runner.commit`), `queue_timeline` (unattended queue + `queue-worker`), `create_timeline` (chains). Server owns `famly_plan_status` / `famly_plan_note` (and legacy `famly_plan_uploaded`); rejections carry stable sub-codes and post one Slack line; held queue jobs are flagged needs_attention.
- Quality check cron: `onboard-quality-check`, read-only, Mon (digest) and Thu, 07:30 London, Slack alerts on new issues, CSV in reports/.
- Reference refresh cron: `reference-refresh`, daily 03:10 London, needs REFERENCE_DIR, validates and backs up before replacing the five generated reference files.
- Test suite guard: tests blank credentials and refuse real network calls (`tests/__init__.py`, `tests/network_guard.py`).
- Prod .env (2026-11-18): COMMIT_ENABLED true, allow-list `*`, QUEUE_MODE auto, ONBOARD_MODE live, BILL_PAYER on, LEGACY_WRITE off. Queue 303 done, 0 held.
- `onboard-audit --preview-estimate` (live 2026-11-18): fees vs Famly's previewed estimate of the active plan (only PUT, preview-only, `actions/plan_estimate`); cron stays GET-only. Plan dates compare the span of all plans (funding/advanced fees split plans): plan_from_differs / plan_to_differs / plan_gap_between_plans.

- Ad hoc (on main unless noted): read-only `adhoc-audit`, `adhoc-dry-run`, `adhoc-probe-read`; test-children-only `create-test-children` / `adhoc-probe-write`; worker steps 1-2 (jobs table, dry mode, Slack gate; ADHOC_MODE default off, prod dry). Step 3a (live writes for an EXISTING child: book, tag, end date) on branch adhoc-writes, fakes only, NOT run live; new-child creation (3b) and the intake webhook are not built. Design: docs/adhoc_design.md.

- Ad hoc worker service (`famly-adhoc-worker`, installed and active): `run_forever` is a robust loop (drain, idle sleep 10 s, errors by type, clean SIGTERM); template `deploy/famly-adhoc-worker.service.example`, NOT installed; restart the unit after editing `.env`.

- Children registry (branch children-registry, stage 1): var/children_registry.sqlite3 (CHILDREN_REGISTRY_PATH), `registry-backfill` (read-only, dry run unless --confirm), `registry-lookup`, `registry-stats`. Fakes only, not run live. Design: docs/registry_design.md.

- Ad hoc + registry (branch adhoc-registry, stages 2-3): registry match before create, operations child booked without tag, registry rows, Slack and HubSpot note/`famly_followup` reports. Fakes only. Needs the HubSpot property `famly_followup` (dropdown) before live.

- Registry stage 4 (branch registry-stage4, fakes only, NOT run live): a nursery deal joins an ad hoc child (registry match before create, same-child Slack question, dates/tag join writes, registry rows). ONBOARD_JOIN default off; probe steps `join-dates` / `untag` first. See docs/registry_design.md.

- Registry nightly refresh (branch registry-refresh, fakes only): `registry-refresh [--dry-run]`, cron 02:30 London (line in docs/registry_design.md); nightly Slack list of every open issue (child name, deal link, how to fix, new ones marked; closed deals left out), branch registry-refresh-issues.

- Gate A rewrite (branch onboard-gate-message, fakes only): names, emails, real dates, Actions/Summary/Warnings; a bill payer email match asks Same / Siblings (registry `sibling_links`) / Different (a NEW payer with the same email). Different is NOT probed live: Famly may refuse a duplicate email, and later deals with that email will flag bill_payer_ambiguous.

- Deposits (`actions/deposit`; probe merged a4ab68d, worker on branch deposit-worker, fakes only): webhook `deposit_enqueue` -> job waits for child, bill payer and plan_created -> Slack Approve -> first deposit + invoice (period = the following Sunday only, due in 7 days; preview must equal the deposit, then Persist, notify on). Deal property `famly_deposit_status`: deposit_added / needs_attention (live + allow-listed only). Famly's own invoice run is at midnight on the 1st (Drew). 50/25/25: later deposits added the day before their date (1st of month), own invoice on that date from 08:00 London unless the regular run bills the child that month. Cron `deposit-worker` every 10 min. `deposit-backfill` (dry unless --confirm): paid stages get one PAID_EXTERN deposit, Deposit Pending is queued, 50/25/25 listed for a hand check. Probe facts (live 2026-11-27): a POST adds, decimals kept, paid reads back PAYED_EXTERNALLY, unpaid PENDING; preview leaves paid deposits out.

## .env switches (names only; values live in .env)
- COMMIT_ENABLED (master switch for Approve commits; dev false) and COMMIT_ALLOWED_CHILD_IDS (allow-list, `*` = all).
- QUEUE_MODE off | dry_run | auto (unset = dry_run), QUEUE_AUTO_COMMIT on | off (default off: every queued plan waits for Slack Approve; QUEUE_ALLOWED_INSTITUTIONS removed 2026-11-27).
- ONBOARD_MODE off | dry | live (default off; dev off), ONBOARD_ALLOWED_DEAL_IDS, ONBOARD_TEST_DEAL_IDS, ONBOARD_ENFORCE_ALLOWLIST (only `off` disables).
- ONBOARD_BILL_PAYER on | off (default off), ONBOARD_BILL_PAYER_DETAIL_SELECTION, ONBOARD_URN_GENERATION on | off (default off; ON in prod and working, confirmed 2026-11-26).
- ONBOARD_JOIN on | off (default off; registry stage 4), ONBOARD_JOIN_LEAVING_REASON_ID (optional, only if the probe shows Famly needs one). CHILDREN_REGISTRY_PATH must reach the web process and the onboard worker.
- PLAN_FLAG_LEGACY_WRITE (default on; `off` stops writing famly_plan_uploaded), ONBOARD_SCOPE_LEAVE_AFTER (default 2026-01-01).
- REFERENCE_DIR (outside the repo, shared by web, workers and cron), PREVIEW_STORE_PATH, ONBOARD_STORE_PATH.
- Secrets (never print): FAMLY_ACCESS_TOKEN, HUBSPOT_TOKEN, SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, INTAKE_SHARED_SECRET.

## Open work and parked
- HubSpot workflow: add the deal record id to the plan_preview webhook body and remove the step that sets famly_plan_uploaded (no flag is written until the id is sent).
- Audit: check that each funded and advanced-fee plan's own start date is accurate (not checked yet).
- Parked: adding a HUBSPOT reference to an EXISTING child (needs a full children.update echo; not built); adding a reference to an existing contact (full contacts update; not built); parent app roles and logins; Shoreditch URN site code.
- Delete when done: temporary `urn_lookup` and `delete_plans` actions; probe commands (`probe-bill-payers`, `probe-child-create`, `probe-contact-create`) once no longer needed.

## Commands used most
- `python main.py onboard-enqueue --deal-id ID [--reopen-done]` queue a deal (`--file PATH --limit N|--all` for a batch).
- `python main.py onboard-dry-run --deal-id ID` plan only, no writes.
- `python main.py onboard-worker [--once]` run the onboarding worker.
- `python main.py onboard-audit --operations [--out CSV]` read-only audit of onboarded deals.
- `python main.py onboard-quality-check [--baseline|--digest]` the cron job.
- `python main.py queue-worker [--once]`, `queue-status`, `queue-list`, `queue-held`, `queue-pause`, `queue-resume`, `queue-cancel`.
- `python main.py plan-check --file ids.txt` read-only check of deals against the plan rules.
- `python main.py reference-refresh [--dry-run]` and `pull-references` refresh reference/ JSON.
- `python main.py hubspot-set-plan-status --from-famly (--operations|--file) [--confirm]` backfill plan status (dry run by default).
- `python main.py chain-status|chain-resume|chain-cancel|chain-sweep` manage create_timeline chains.

## To confirm
- Prod ONBOARD_ENFORCE_ALLOWLIST (screenshot cut off at `of…`, likely off = allow-list not enforced). 34 held jobs in var/onboard_jobs.sqlite3 (last 10 Nov).
- Whether the 2026-10-28/29 work (plan status, rejection sub-codes, held-queue visibility, reference-refresh and its cron, pricing-input guards) has been deployed and exercised live; those entries say "fakes only, NOT run live".
- Whether the quality-check cron runs at 07:30 London or UTC on the droplet (cron may ignore CRON_TZ; switch to 06:30 on 2027-03-28 if UTC).
- Open in the archive: month-end group boundary rule, `var/urns_missing.txt`, sibling deals sharing one Bill Payer label.
