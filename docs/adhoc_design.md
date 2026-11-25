# Ad hoc parents and sessions into Famly: design (draft, read-only phase)

Status: phase 1 (`adhoc-audit`, read-only) and `create-test-children` built on branch `adhoc-design`, tested with fakes only, NOT run live. No other Famly writes.

## Goal
Get ad hoc parents/children into Famly, and put their booked sessions in as well.

## What we know
**HubSpot (schema read 2026-11-19, no records read)**
- A deal pipeline "Ad Hoc" (id 865556791). Stages: Enquiry, Form Submitted, Requested Days Unavailable, Approved, Invoice Sent, Invoice Paid, Ad Hoc Started, Ad Hoc Completed, Closed Lost.
- Booking data sits on the deal: `ad_hoc_date_1..10` (date), `ad_hoc_session_1..10` (Morning | Afternoon | Full Day), `ad_hoc_end_date`, `adhoc_discount` (number). So up to 10 dated sessions per deal, each a date plus a slot.
- The existing onboarding worker only accepts Operations / Deposit Pending (`actions/onboard/worker.py`), so ad hoc deals are never picked up today.

**Famly / this repo**
- Ad hoc bookings are Famly **products**, not recurring plan sessions (per Drew).
- `reference/products_catalogue.json` holds 8 products per site (HDCITY, HDCW, HDWEST); none is ad hoc. Either the pull excludes them, or they are not set up per site yet. Unknown.
- Many ad hoc children were in EYWorks, so they already exist in Famly **without a plan**. The onboarding worker creates children and contacts but never updates, so it must not run blindly on these: it would create duplicates unless child lookup finds them first (`integrations/child_lookup.py`, `famly_childid` on the deal).

## Open questions (need answers before building)
1. How does Famly book a product against a child on a date? Which API or mutation, and does it take a date, a date range, or a quantity? (Probe needed, read-only first.)
2. Are there separate ad hoc products per slot (Morning / Afternoon / Full Day) and per site, with their own prices? Or one product with a quantity? What are their ids?
3. Does `adhoc_discount` need to reach Famly (as a discount, or a price override), or is it only for the HubSpot invoice?
4. Which stage means "go"? Candidates: Approved, Invoice Paid, Ad Hoc Started. Invoice Sent / Paid affects whether we book before payment.
5. What happens with a child who has a Famly record but no HubSpot `famly_childid`? Match by what, and who confirms?
6. Do ad hoc children get a contact and bill payer, same as onboarding? Same rules (bill payer first, contacts independent)?
7. Is a second booking for the same child a new deal, or an edit of the same deal's dates? Edits after booking need a rule (we never delete in Famly).

## Proposed phases
1. **Read-only audit** (`onboard-audit`-style command, GET only): for every deal in the Ad Hoc pipeline, report stage, number of sessions, whether `famly_childid` is set, whether the child is found in Famly by existing lookup, and date sanity (missing slot, date in the past, end date earlier than last date). Output a CSV in `reports/`; child ids only, no names.
2. **Probe** (needs Drew's approval, run from /home/drew/famly): how Famly represents an ad hoc product booking for one known test child; capture the request shape. One-off probe command, like `probe-child-create`.
3. **Children and contacts**: reuse the onboarding worker with an ad hoc deal gate. Lookup-first so EYWorks-derived children are linked, not duplicated. Creates only.
4. **Sessions**: a booking writer for the product bookings, through the same Slack approval and queue guard as plan writes (preview, Approve, commit). Hard error on any missing product id, slot or date; never guess an id.
5. Reference: extend `pull-products` to include and map the ad hoc products (slot x site -> product id), validated like the other reference files.

## Rules carried over
- No names in logs, Slack or the jobs table; child ids only. Onboarding names exception applies only to the onboarding pipeline run.
- Hard errors for anything that could misprice a booking; warnings only for cosmetic issues.
- Famly writes only through the approved paths; no write or delete outside tests until Drew says so.
- Famly plan `to` is exclusive and segments inclusive; check whether product bookings use the same convention.

## Next step
Phase 1 audit needs no new Famly access and answers questions 5 and part of 4. Phase 2 needs a probe approved by Drew.

## Built so far
- `python main.py adhoc-audit [--file ids.txt] [--stage NAME] [--out reports/adhoc.csv] [--codes A,B]`: GET-only. Codes per deal: no_sessions, session_slot_missing / session_date_missing / session_slot_unknown / session_date_invalid / session_date_duplicate / session_date_past (not for Started, Completed, Closed Lost) / session_date_weekend, end_date_missing / end_date_invalid / end_date_before_last_session, site_unmapped, child_missing / child_ambiguous / famly_childid_not_in_famly, child_not_at_site, session_outside_enrolment, child_has_plan / child_no_plan, plans_not_readable. Summary has counts by code, stage and site; CSV has deal id, child id and codes only.
- `python main.py create-test-children --confirm`: one AATest child per site (HDCITY, HDCW, HDWEST), isTestChild true, Hatchlings group, idempotent by URN reference `AATEST-<SITE>`, creates only (no update or delete), at most 3 creates, ids to var/test_children.json. Run from /home/drew/famly, with approval.

## Famly requests captured from the web app (2026-11-24, HDCW test child)
From Drew's browser-network screenshots (kept in the gitignored `screenshots/` folder). These are the web app's INTERNAL endpoints (`app.famly.co/graphql?<OperationName>` and `app.famly.co/api/v2/...`), not the public API. Not yet tried from this codebase; whether our access token works on them is unknown. The internal GraphQL client the probes use (`GraphQLClient()`) is the likely route.

**Correction to the first draft:** ad hoc bookings are not "products". They are **session bookings** of standalone sessions (`SessionVersion`, type `StandaloneSession`, titled Morning / Afternoon / Full Day). `productBookings` is sent empty. Each has one `sessionAvailabilityId` per weekday (MONDAY..), so a booked date picks the availability whose `day` matches its weekday.

1. **Look up sessions and prices (read):** query `GetSessionsAndProducts(childIds, validOnDate)` -> `finance.revenueItems.productVersions` and `.sessionVersions`; each session has `sessionId`, `sessionVersionId`, `title`, `sessionAvailabilities[{day, sessionAvailabilityId, timeRange}]` and `pricing[{adHocPrice, period, pricingGroupId, volume}]`. Seen: Morning 75.00, Afternoon 75.00, Full Day 150.00; TWO pricing groups exist per session with different prices in places, so the child's pricing group must be chosen deliberately (hard error if unknown; never guess).
2. **Check conflicts (read):** query `GetAdHocPurchaseSessionsConflicts(childId, purchases, date)` -> `finance.purchases.previewUpdate` -> `newPurchaseConflictMessages` and `unbookableSessions`. A preview only; the pre-check before any booking.
3. **Book (write):** mutation `UpsertPurchase(purchases: [PurchaseInput!]!)` with `purchases: [{childId, discountAmount: "0", productBookings: [], sessionBookings: [{sessionAvailabilityId, purchaseDate: "YYYY-MM-DD", pricingGroupId, bookedPrice: "150.00"}, ...]}]`. One booking per date. `discountAmount` is the place for `adhoc_discount` (to confirm).
4. **Undo (write):** mutation `DeletePurchase(purchaseIds)`. Seen only for cleaning up a test booking. Never to be used on a real child.
5. **Tag a child (write):** `POST /api/v2/tags/<tagId>/assign` with body `{"childId": "<id>"}`. The tag id seen is `3fe0c824-9f25-4b91-a1f3-4e16d31dbe53` (to confirm this is the "ad hoc only" tag and whether each site has its own).
6. **Remove or set the end date (write):** mutation `EditChildActiveDates(childId, childActiveDates: {startDate, endInfo?: {endDate, reasonForLeavingId, additionalInfo}}, isParent: false)`. Sending only `startDate` (no `endInfo`) REMOVES the end date. Sending `endInfo` sets one and needs a `reasonForLeavingId`.

## Decisions from Drew (2026-11-24)
- Every ad-hoc-only child gets a Famly tag saying so.
- Ad hoc children keep NO explicit end date: Famly sets it automatically to 31 August after the 4th birthday, and it stays that way unless the parent submits a leave form. This differs from the onboarding rule (end date always sent), so it applies to the ad hoc path only and must not change `actions/onboard`.

## Still open
- Confirm the tag id(s); how to list tags read-only.
- Confirm Famly really applies the 31 August default when the end date is removed (verify on a test child, read the summary back).
- Which pricing group applies to a child; how `adhoc_discount` maps to `discountAmount`.
- Whether the token works on the internal endpoints; the existing children with an end date from EYWorks (remove it or leave it?).

## Probe results (2026-11-24)
- Internal endpoints work with our token: `GetSessionsAndProducts` and the conflict preview (`adhoc-probe-read`, run live, read-only).
- Ad hoc sessions are the plain Morning 75.00 / Afternoon 75.00 / Full Day 150.00; the "(F)/(FNA)/..." variants have no ad hoc price. Both pricing groups per site carry the same price. Booking one session on a date makes the child's other sessions that date unbookable (`ANOTHER_INPUT_PURCHASE`): one session per child per date.
- Pricing groups: HDCW `eebac1d0-...` = under 3 (Drew); `d31d08ee-...` assumed over 3. HDCITY (`18b50271-...`, `78d46103-...`) and HDWEST (`3773609b-...`, `3be14e3c-...`) NOT mapped yet: a booking there is a hard stop.
- Tags: `GET /api/v2/tags?institutionId=<site id>` lists a site's tags (`tagId`, `title`, `institutionId`, `deletedAt`); the tag title is "Ad Hoc Only". A child's summary `tags` shows what it carries.
- The automatic end date is NOT visible through any API we read (REST summary `endDate` None, public `sitesRelation.lastDay` empty, internal `childrenList.endDate` None), even on a real child with no end date. Treat "no end date" as the correct ad hoc state; confirm in the Famly UI.
- `adhoc-probe-write --confirm --step end-date|tag|booking` (test children only) is the write stage; see actions/adhoc_probe/writer.py.

## Pipeline rules (Drew, 2026-11-24)
- A new HubSpot deal is created for EVERY ad hoc request, so one child can have several ad hoc deals. Match the child by URN, then parent email, then child name (accepted only if name/date of birth do not contradict the deal; a parent email that matches only a differently named child is a HOLD).
- No plan in Famly + ad hoc request = "ad hoc only": tag, end date removed. A child WITH a plan is a regular child: no tag, no end-date change.
- Go stage is **Approved**. A HubSpot workflow will POST the deal id to the intake webhook when an ad hoc deal reaches Approved.
- First live version is a DRY RUN on a couple of deals before any write (`adhoc-dry-run`, read-only, built; see actions/adhoc_plan).
- Sessions after the end of the child's Famly enrolment (all 8 such live deals): removing the end date fixes the no-plan children; children WITH a plan and an enrolment that has ended need a decision.
- `adhoc_discount` is a HOLD (`discount_unconfirmed`) until we know whether it is an amount or a percent, and per session or per deal.

## Decisions and findings (2026-11-24, evening)
- Match-safety rule: when a parent email matches only a differently named / dated child, the pipeline must ASK IN SLACK "is this the correct child?" (buttons: that child / create new). No names in Slack: deal id and a Famly child link only. (Slack gate not built yet; the dry run currently reports it as the hold `parent_email_matches_a_different_child`.)
- Child who has left (enrolment ended, has a plan): first try to book the session as is. Read-only evidence: the conflict preview raises no conflict for a session after the enrolment end date (two real children). To be proven by a real booking on the HDCW_LEFT test child (`adhoc-probe-write --step create-variants --step left-booking`). If Famly refuses, move the end date to cover the session but make the plan end on the SAME day the end date used to be.
- `adhoc_discount` is a PERCENT and applies only to the ad hoc sessions being added. Famly's `discountAmount` may be an amount, not a percent: the `discount` probe step books with discountAmount "10" and reads back `discountAmount` and `total` to see what Famly does. Until then the planner holds `discount_unconfirmed`.
- New children are created WITHOUT an end date; if Famly rejects that, create then remove the end date (`create-variants` tries it on HDCW_NOEND).
- First dry run (4 real deals, 2026-11-24, read-only): all 4 held. 3 by `discount_unconfirmed`, 1 by `parent_email_matches_a_different_child` (a likely sibling), 1 by `session_date_past`. Prices and pricing groups came out right (75/75/150; under/over 3 by age on each date).

## Probe results, round 2 (2026-11-24, test children)
- A child created with NO `lastDay` is accepted (`HDCW_NOEND`: endDate None). No create-then-remove fallback needed.
- A session booked AFTER a child's enrolment ended (`HDCW_LEFT`, ended 2026-06-30, booked 2026-12-28) is accepted. For leavers: just add the session; do not move the end date. Booking BEFORE an enrolment starts is untested, so the planner still holds it.
- Discount: `discountAmount "10"` on a 75.00 booking came back `10.00`, total `65.00`: a FLAT amount off the purchase.
- On HubSpot `adhoc_discount` is a FRACTION (live values 0.025, 0.05, 0.1, 0.15, 0.325, 0.45, 0.5), so discountAmount = ad hoc total x fraction, rounded half up to the penny. Values outside 0..1 are a hold (`discount_invalid`).
- Test children now: HDCITY, HDCW, HDWEST, plus HDCW_NOEND and HDCW_LEFT (ids in var/test_children.json).

## Ended enrolments are NOT a hold (Drew, 2026-11-24)
Famly accepts a booking after an enrolment has ended (proved on a test child), so the session is simply added, for plan and no-plan children alike; the end date is not moved. A session BEFORE the enrolment starts is untested and stays a hold (`child_starts_after_first_session`). Names may be printed to the terminal (never logged).

## Worker, step 1 (built, fakes only): store + worker loop, dry mode
- `actions/adhoc/`: its own jobs table (`var/adhoc_jobs.sqlite3`, `ADHOC_STORE_PATH`), same conventions as onboarding (one active job per deal by a unique partial index, atomic claim, in-flight `writing` jobs become `needs_review` and are never replayed). One child can have many deals, each its own job.
- `ADHOC_MODE` off | dry | live (default off). `dry` plans each deal READ-ONLY (the planner in `actions/adhoc_plan`, wired by `adhoc_plan/readers.py`) and records plan, holds and flags: `dry_done` or `held`. `live` is refused until the write step exists.
- A webhook job must be an Approved deal in the Ad Hoc pipeline, else `held` `stage_not_eligible`. A CLI job may be any stage (the plan shows the flag `stage_not_approved`).
- Commands: `adhoc-enqueue --deal-id ID`, `adhoc-worker [--once]`, `adhoc-status [--deal-id ID]`.
- Order of the remaining steps: 2 Slack gate (plan approval + the child-match question with child name and deal id), 3 writes behind `live`, 4 the intake webhook last.

## Worker, step 2 (built, fakes only): the Slack gate in dry mode
- Same channel as onboarding (SLACK_CHANNEL_ID), same signed `/slack/interactivity` route; anyone in the channel may click. New button ids: `adhoc_approve`, `adhoc_reject`, `adhoc_match_yes`, `adhoc_match_new`, `adhoc_retry` (`integrations/slack.py`, routed in `server.py` and `web_registry.py`; `famly-intake` needs a RESTART after the merge).
- Clean plan -> "Ad hoc approval" message (deal, child, who, each session and price, total, discount, to pay, steps, plan hash) with Approve / Reject; the job waits `awaiting_approval`. Approve re-queues; the worker re-plans and, if the hash is unchanged, ends `dry_done` (nothing written in dry mode; live mode will write here). A plan that changes after approval is asked again once, then `needs_review` (`plan_unstable`).
- Holds -> a plain-English "not done, a person needs to check" notice with a Retry button (a fresh job, same enqueue rules).
- A lone `parent_email_matches_a_different_child` with exactly one candidate -> the CHILD-MATCH question with the Famly child's name and the deal id (the one message that carries a name; the name comes from memory at post time and is never stored). "That's the child" re-plans with that child confirmed; "Create a new child" re-plans as a new child; Reject holds the job. A deal's own URN / recorded id / reference match is never overridden by this answer.
- Every click is an atomic state change with a per-request nonce and a plan-hash prefix, so a double click or an old message does nothing. If Slack refuses a post the job is held `slack_post_failed` rather than left waiting.

## Hold notices name the dates (Drew, 2026-11-24)
A hold now says WHICH dates it is about: past, weekend and duplicate dates, and for a Famly booking conflict each conflicting date with its kind (e.g. `ALREADY_BOOKED`, or "another session on this booking is on the same day"). The plan carries `details` (ISO dates and conflict kinds only; no names) and the hold notice prints them as `28 Dec 2026`.

## Worker, step 3a (built, fakes only): live writes for an EXISTING child
- `ADHOC_MODE=live` now runs the worker; there is no deal allow-list (Drew): the control is what is enqueued plus the Slack Approve on every plan. `actions/adhoc/writes.py` holds the only Famly writes: book (`UpsertPurchase`), tag (`POST /v2/tags/<id>/assign`) and end-date removal (`EditChildActiveDates`, startDate only). No delete or booking update exists in the package.
- The plan carries the EXACT purchase (session availability ids, prices, pricing groups, flat discount amount) and the site id, so the plan hash covers precisely what is sent; a live job writes only if the re-plan after the Approve click has the same hash.
- Order: BOOK first (the money step), then tag and end date (only for an ad hoc only child; a standard child is booked and otherwise untouched). Each request is sent ONCE, never retried:
  - booking refused (GraphQL errors with no data, or HTTP 4xx) -> `failed`, nothing booked;
  - booking outcome unclear (timeout, 5xx, errors WITH data) -> `needs_review` `booking_outcome_unknown`, never re-sent (a later Retry re-plans and Famly's conflict preview then reports a session that is already booked);
  - the answer is checked against the approval (rows, prices, pricing groups, discount); a difference -> `needs_review` `booking_mismatch`;
  - the tag and the end date are settled by READING the child afterwards, never re-sent; a problem -> `needs_review` with the code.
- `write_sent` is persisted atomically BEFORE the booking is sent, so a job never books twice; a job caught mid-write is `needs_review` `interrupted` and is never replayed.
- Step 3b (built, fakes only, NOT run live): a NEW child is created in live mode after the Slack approval (child with no end date, contacts bill payer first, bill payer stage, recipient link, URN from the onboarding allocator), then booked and tagged; `new_child_not_built` is gone. Design and decisions: docs/adhoc_3b_design.md.
- No HubSpot write-back yet (no famly_childid / status property is written to the deal).

## FUTURE WORK (flagged, not built): nursery onboarding must JOIN a child that ad hoc already created
Why: an ad hoc child is created by the ad hoc worker with the ad hoc deal's HUBSPOT reference, its own URN, no end date and the Ad Hoc Only tag. When that family later signs up for nursery, the onboarding worker (`actions/onboard/worker.py` `_ensure_child`) looks for a child by the NURSERY deal's reference, then by the deal's URN, and otherwise CREATES one. If it does not find the ad hoc child, Famly gets a duplicate child.
What to build later (onboarding branch, a separate task, not part of ad hoc):
1. Before onboarding creates a child, look for a child that was created for ad hoc: by the deal's URN, by parent email, by name + date of birth, or by the ad hoc deal's allocated URN (the onboard store records it per deal). Found exactly once -> JOIN it instead of creating; more than one or unsure -> a Slack question (like the ad hoc child-match question), never a guess.
2. Joining means: use that child; add the Operations deal's HUBSPOT reference as the child's FIRST deal reference, since an ad hoc child carries none (the parked item "adding a HUBSPOT reference to an EXISTING child needs a full children.update echo; not built"); set the enrolment to the nursery dates (the ad hoc child has NO end date, but onboarding always sends one: the deal's end date or the 31 August after the 4th birthday); remove the Ad Hoc Only tag; then the normal plan write. Contacts and the bill payer already exist: onboarding must treat them as present, not create them again.
3. The reverse also matters: a child created by onboarding who later asks for ad hoc days is an existing child with a plan (a standard child), which the ad hoc worker already handles.
Open questions for Drew when this is picked up: which match signals are enough to join automatically; whether the Slack question is needed every time or only when unsure; what to do with the tag and with already-booked ad hoc sessions.

## Ad hoc children carry no deal reference (Drew, 2026-11-25)
A new ad hoc child is created with its URN only (`externalId` + URN reference); the ad hoc deal id is NOT put on the Famly child (contacts keep their HubSpot contact reference). The URN, allocated once per deal by the onboarding allocator and not written to the deal, is the key for the pre-create check, the settle after an unclear create, the read-back, and a Retry finding a child an earlier job made. A second ad hoc deal for the same family matches the existing child by parent email and by name + date of birth. If the family joins nursery, onboarding adds the Operations deal id as the child's first reference.
