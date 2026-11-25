# Tasks (task, branch, status). Kept current by the coordinator; newest first.

| Task | Branch | Status |
|---|---|---|
| Children registry (var/, gitignored) + rule edits; workers check it before creating; same-child Slack question; operations joins ad hoc child; ad hoc books for operations child | children-registry, registry-deal-list | Stage 1 merged (e750f5d). Backfill now also lists deals with no Famly child (--deals-out, closed stages left out; never stored). Registry filled in prod (353 children); gap list in prod reports/registry_to_check_2026-11-25.csv. Name rules merged; Weekend School deal type (branch registry-weekend-school). Stages 2+3 for the ad hoc worker built on adhoc-registry (fakes only), reviewed and approved (a3cc19b). Then: Drew creates famly_followup; live test. Stage 4 (operations joins ad hoc child) later. |
| Ad hoc new-child fixes: fresh roster read-back, URN and child id written to the deal, resume contacts | adhoc-newchild-fixes | Merged (reviewed, approved). Next: Drew restarts `famly-adhoc-worker`, then recover deal 67661137730 (child HD0001CL exists in Famly with no parent and no booking). |
| Install the `famly-adhoc-worker` systemd unit | none | Drew: copy `deploy/famly-adhoc-worker.service.example`, confirm paths with `systemctl cat famly-onboard-worker`, enable and start it; restart it after any `.env` change (ADHOC_MODE is read at start). Until then approvals only run when `adhoc-worker --once` is run by hand. |
| Ad hoc worker as a service (robust loop, SIGTERM, unit template, docs; needs_review after a sent booking) | adhoc-worker-service | Merged and deployed (main d1b5739). Reviewed twice, approved. |
| Ad hoc live test on the HDCW test child (deal 68715656456) | none | Passed: booked 4 sessions, tag present, end date none. The 4 test sessions were removed by hand in Famly by Drew. |
| FUTURE: nursery onboarding must join a child already created for ad hoc (no duplicate; add the nursery deal reference, set nursery dates, remove the Ad Hoc Only tag) | not started | Flagged in docs/adhoc_design.md ("FUTURE WORK"). Separate onboarding task, after the ad hoc pipeline is live. |
| Ad hoc worker 3b: new child, contacts, bill payer and URN (child carries no deal reference; URN is the key) | adhoc-new-child | Merged and deployed (main 898decf). First live deal exposed the read-back bug, fixed in adhoc-newchild-fixes. |
| Ad hoc intake webhook (Approved stage -> deal id) and backfill (`adhoc-enqueue --future`) | adhoc-webhook-backfill | Merged and deployed (main f2a7ae6). Drew creates the HubSpot workflow. |
| Ad hoc worker 3a: live writes for an existing child (book, tag, end date) | adhoc-writes | Merged and deployed (main 4f43196); exercised live on a test child. |
| Ad hoc worker 2: Slack gate (plan approval, child-match question, hold notices, conflict dates) | adhoc-slack-gate and later | Merged, deployed, exercised. |
| Ad hoc worker 1: jobs table and dry-mode loop | adhoc-worker-store | Merged, deployed. |
| Ad hoc planner, audit, probes, test children (HDCITY, HDCW, HDWEST) | several | Merged. |

Open items: no `adhoc-cancel` command (stale queued jobs are re-planned, harmless); no HubSpot write-back yet (famly_childid, status); delete the probe commands once the pipeline is proven; ExceptionAfterWriteTests in tests/test_adhoc_worker.py re-runs the live tests (reviewer note, harmless).
