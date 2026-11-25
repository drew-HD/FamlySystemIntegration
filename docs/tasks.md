# Tasks (task, branch, status). Kept current by the coordinator; newest first.

| Task | Branch | Status |
|---|---|---|
| Install the `famly-adhoc-worker` systemd unit | none | Drew: copy `deploy/famly-adhoc-worker.service.example`, confirm paths with `systemctl cat famly-onboard-worker`, enable and start it; restart it after any `.env` change (ADHOC_MODE is read at start). Until then approvals only run when `adhoc-worker --once` is run by hand. |
| Ad hoc worker as a service (robust loop, SIGTERM, unit template, docs; needs_review after a sent booking) | adhoc-worker-service | Merged and deployed (main d1b5739). Reviewed twice, approved. |
| Ad hoc live test on the HDCW test child (deal 68715656456) | none | Passed: booked 4 sessions, tag present, end date none. The 4 test sessions were removed by hand in Famly by Drew. |
| FUTURE: nursery onboarding must join a child already created for ad hoc (no duplicate; add the nursery deal reference, set nursery dates, remove the Ad Hoc Only tag) | not started | Flagged in docs/adhoc_design.md ("FUTURE WORK"). Separate onboarding task, after the ad hoc pipeline is live. |
| Ad hoc worker 3b: new child, contacts and bill payer (reuse onboarding building blocks) | not started | Next. Until built, a plan that needs a new child is held `new_child_not_built` in live mode. |
| Ad hoc intake webhook (Approved stage -> deal id) | not started | After 3b. Drew creates the HubSpot workflow. |
| Ad hoc worker 3b: create a NEW child, contacts, bill payer and URN, then book and tag | adhoc-new-child | Built, fakes only, awaiting review; live test on a test deal not run. |
| Ad hoc worker 3a: live writes for an existing child (book, tag, end date) | adhoc-writes | Merged and deployed (main 4f43196); exercised live on a test child. |
| Ad hoc worker 2: Slack gate (plan approval, child-match question, hold notices, conflict dates) | adhoc-slack-gate and later | Merged, deployed, exercised. |
| Ad hoc worker 1: jobs table and dry-mode loop | adhoc-worker-store | Merged, deployed. |
| Ad hoc planner, audit, probes, test children (HDCITY, HDCW, HDWEST) | several | Merged. |

Open items: no `adhoc-cancel` command (stale queued jobs are re-planned, harmless); no HubSpot write-back yet (famly_childid, status); delete the probe commands once the pipeline is proven; ExceptionAfterWriteTests in tests/test_adhoc_worker.py re-runs the live tests (reviewer note, harmless).
