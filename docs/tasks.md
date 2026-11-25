# Tasks (task, branch, status). Kept current by the coordinator; newest first.

| Task | Branch | Status |
|---|---|---|
| Ad hoc new-child fixes: fresh roster read-back, URN and child id written to the deal, resume contacts | adhoc-newchild-fixes | Built, tests running. Needs a reviewer pass (Famly and HubSpot writes), Drew's merge approval, then restart `famly-adhoc-worker`. Then recover deal 67661137730 (child HD0001CL exists in Famly with no parent and no booking). |
| Install the `famly-adhoc-worker` systemd unit | none | Drew: copy `deploy/famly-adhoc-worker.service.example`, confirm paths with `systemctl cat famly-onboard-worker`, enable and start it; restart it after any `.env` change (ADHOC_MODE is read at start). Until then approvals only run when `adhoc-worker --once` is run by hand. |
| Ad hoc worker as a service (robust loop, SIGTERM, unit template, docs; needs_review after a sent booking) | adhoc-worker-service | Merged and deployed (main d1b5739). Reviewed twice, approved. |
| Ad hoc live test on the HDCW test child (deal 68715656456) | none | Passed: booked 4 sessions, tag present, end date none. The 4 test sessions were removed by hand in Famly by Drew. |
| FUTURE: nursery onboarding must join a child already created for ad hoc (no duplicate; add the nursery deal reference, set nursery dates, remove the Ad Hoc Only tag) | not started | Flagged in docs/adhoc_design.md ("FUTURE WORK"). Separate onboarding task, after the ad hoc pipeline is live. |
| Ad hoc worker 3b: new child, contacts, bill payer and URN (child carries no deal reference; URN is the key) | adhoc-new-child | Built and reviewed (one blocker found and fixed). Awaiting Drew's merge approval, then pull and restart `famly-adhoc-worker`. First live check: the first real new-child deal, with a Slack Approve. |
| Ad hoc intake webhook (Approved stage -> deal id) | not started | After 3b. Drew creates the HubSpot workflow. |
| Ad hoc worker 3b: new child, contacts, bill payer and URN (child carries no deal reference; URN is the key) | adhoc-new-child | Built and reviewed (one blocker found and fixed). Awaiting Drew's merge approval, then pull and restart `famly-adhoc-worker`. First live check: the first real new-child deal, with a Slack Approve. |
| Ad hoc worker 3a: live writes for an existing child (book, tag, end date) | adhoc-writes | Merged and deployed (main 4f43196); exercised live on a test child. |
| Ad hoc worker 2: Slack gate (plan approval, child-match question, hold notices, conflict dates) | adhoc-slack-gate and later | Merged, deployed, exercised. |
| Ad hoc worker 1: jobs table and dry-mode loop | adhoc-worker-store | Merged, deployed. |
| Ad hoc planner, audit, probes, test children (HDCITY, HDCW, HDWEST) | several | Merged. |

Open items: no `adhoc-cancel` command (stale queued jobs are re-planned, harmless); no HubSpot write-back yet (famly_childid, status); delete the probe commands once the pipeline is proven; ExceptionAfterWriteTests in tests/test_adhoc_worker.py re-runs the live tests (reviewer note, harmless).
