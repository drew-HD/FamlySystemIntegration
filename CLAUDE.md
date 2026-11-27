# Famly integration
- Read docs/status.md at the start of a session; open the archive only when a task needs history.
- Python, stdlib unittest. One module: python -m unittest tests.test_x -q. Full suite: python -m unittest discover -s tests -q (slow; once at the end).
- Layout: actions/<name>/{runner.py,cli.py,web.py}; web_registry.py routes webhook actions; core/ clients; integrations/ Slack and catalogues; reference/ JSON (pulled by pull-* commands); var/ is gitignored personal data.
- Never log child or parent names. Child ids only in logs. Exceptions are in the bullets below; names may be printed to the terminal.
- Exception for the onboarding pipeline: names, emails, phone numbers, addresses and dates of birth may be read from HubSpot at run time and held in memory for a single call; never print, log or put in the jobs table. Storage and Slack follow the two exceptions below.
- Exception, storage (Drew, 2026-11-24, extended 2026-11-25): names (parent and child), child dates of birth, emails, URNs, Famly ids and HubSpot deal ids (operations and ad hoc) may be kept on the server, but only in a gitignored folder (reports/, var/). This includes the children registry (var/), which the onboarding and ad hoc workers may read and write. Never commit them or write them to logs or the jobs tables. Names may be printed to the terminal (not emails or other contact details).
- Exception, Slack (Drew, 2026-11-24, extended 2026-11-25, made general 2026-11-27): child names are fine in Slack generally (e.g. the ad hoc child-match question and plan approval, the registry same-child question, the join summaries, the nightly registry issue list), with the deal id or link so a person can check the child. Parent names and emails are fine in Slack messages too (Drew, 2026-11-27; e.g. the onboarding approval lists the contacts to be created), read into memory for the message only. Never phone numbers, addresses or dates of birth in Slack. Names and emails never go in logs or the jobs tables.
- Hard errors for anything that could misprice a plan; warnings only for cosmetic issues. Never guess a missing id.
- Do not touch the commit path (approval.py, runner.commit) unless the task says so. Never call a write or delete against Famly outside tests.
- The only other Famly writers are the onboarding worker (`actions/onboard`: child and contact CREATES, plus the registry stage 4 JOIN of an ad hoc child: its active dates (start only ever moved earlier, end date with reason "Other") and removal of the Ad Hoc Only tag, never the group or sessions, behind ONBOARD_JOIN; live mode, allowlisted deals, Slack Gate A), the deposit pipeline (`actions/deposit`: deposit ADDS and invoices only, never edits or deletes; DEPOSIT_MODE live (no deal allow-list), Slack approval for the first deposit, later installments on schedule; every invoice previewed and held unless its total is exactly the deposit; `deposit-backfill --confirm` adds paid deposits) and the one-off probe commands. Onboarding rules: an end date is always sent (deal value or the 4th-birthday default, written back only when empty); HubSpot never blocks Famly (missing properties are warned about and skipped); summary comparisons are flags, never stops; contacts are tried independently, bill payer first.
- Famly's plan `to` is exclusive; segments are inclusive internally.
- Tests use fakes. Hit live Famly only when the task says to verify live.
- Testing: for money, funding, dates, commit and delete guards and queue safety, test thoroughly. For everything else write at most about 8 focused tests. No test-breaking checks unless the task asks. Run only the affected modules with -q, and the full suite once at the end, using: python -m unittest discover -s tests -q 2>&1 | tail -5

## Workflow
- Build in /home/drew/famly-dev (dev .env has no real tokens; COMMIT_ENABLED=false, ONBOARD_MODE=off). Nothing runs from here.
- /home/drew/famly is production: services run from it. Never edit it; only git pull there after I merge.
- Live commands (probes, audits, verify runs) are run from /home/drew/famly, only when the task says so, and I approve each one.
- Work on a branch per task; commit there; never push to or merge main. I review and merge.
- After a merge I pull in prod and restart famly-intake, famly-queue-worker and famly-onboard-worker when runtime code changed.
- Command results and audit files go in reports/ (gitignored), never loose in the project root.
- Logs: journalctl -u <service> (server time is UTC).
