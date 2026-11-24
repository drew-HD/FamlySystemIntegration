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
