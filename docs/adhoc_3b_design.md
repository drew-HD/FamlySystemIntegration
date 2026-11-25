# Ad hoc step 3b: create a NEW child (design with Drew's decisions, 2026-11-25)

Goal: when an approved ad hoc plan needs a new child, create the child (NO end date), its contacts and the bill payer in Famly, give it a URN, then book, then tag. Remove the live-mode hold `new_child_not_built`. One Slack approval, no allow-list. Dry mode behaviour is unchanged.

## Reuse (do not import the onboarding job store or plan format; do not refactor actions/onboard)
- As is: `famly_writes.create_child/create_contact` (+ `CreateRejected`, `OutcomeUnknown`), `inputs.build_contact_input`, `bill_payer_writes.run_stage(public, scan, contacts, approved, site_id, child_id, lookup, unresolved)`, `link_recipient`, `famly_writes.add_contact_relation`.
- `approved` for run_stage = `bill_payer_scan.decide(contacts, payers, None)` computed at PLAN time and stored in the plan (the hash covers it).
- URN: use the onboarding allocator (actions/onboard/urn_alloc.py and the onboard store's `allocate_urn`) so numbers never duplicate. Allocate at plan time in live mode, as onboarding does; put it on the Famly child (`externalId` + URN reference). NOT written back to the HubSpot deal (these deals are one-off). Read how ONBOARD_URN_GENERATION gates it and respect that; if generation is off, hold with a clear code rather than guess.
- NEW `actions/adhoc/child_input.py::build_adhoc_child_input(deal, deal_id, site_id, group_id, first_day, urn)`: same payload as `inputs.build_child_input` but NO `lastDay`, no `isTestChild`, and NO HubSpot deal reference (Drew, 2026-11-25): the child carries only its URN as `externalId` + URN reference. The URN is the key for every lookup (pre-create check, settle after an unclear create, read-back) and for a Retry finding a child an earlier job made (`Deps.allocated_urn`, from the onboard store).
- NEW `actions/adhoc/new_child.py`: a slim port of onboarding's `_ensure_child`, `_create_contacts`, `_create_one` (bill payer first, each contact independent, `locate_contact` check, settle by reads). Share only pure helpers.
- NEW store columns (migration): `child_create_sent` (set atomically BEFORE the create), outcomes entries `contact:<hubspotId>:<code>`, `billpayer:<code>`.

## Inputs and plan
- firstDay = first BOOKED session date. Group = `derive.group_id_for(site, derive.group_title_for_age(completed months at firstDay))`, stored in the plan as `groupId`. HOLD (never guess) on: missing group, DOB, a contact email, or no bill payer contact (`bill_payer_missing`).
- The Slack gate for a new child shows: "Child doesn't exist in Famly, to be created", group, start date, "no end date", contact count (bill payer marked; NO contact names), bill payer decision (create / attach / present), URN to be assigned, then the sessions and prices as today.

## Write order (every request sent once, never retried)
1. Live re-lookup for an existing child by the allocated URN (no URN in the plan: `needs_review urn_missing`, nothing sent). Found -> `needs_review plan_changed` (never write to a child the approval did not name).
2. `child_create_sent`, then create_child. Rejected/Unknown: settle by lookup on the child's URN: found once = created; none + Rejected + readable = `failed`; none + Unknown or unreadable = `needs_review child_outcome_unknown`; more than one = `needs_review child_ambiguous`. Record `child_id` at once; read-back by URN, else `needs_review readback_child_lookup_failed`.
3. Contacts (bill payer first), 4. bill payer stage, 5. recipient link and RELATION update (flags only).
6. Booking: fill the purchase `childId` AFTER creation (the plan hash has it as None). ALWAYS attempted once the child is confirmed, even if a contact failed. Failures as in 3a.
7. Tag. The end-date step is skipped (a new child has none).
- Any contact, bill payer, recipient or relation problem: carry on, end `needs_review`, and post a Slack notice listing the codes (no names).
- Crash between child create and booking: `recover_in_flight` -> `needs_review interrupted`, `child_id` already recorded, no replay. A Retry is a fresh job that finds the child by the deal's allocated URN (an existing-child plan) and books it; missing contacts become a flag `contacts_missing` for a person.
- Slack "Create a new child" answer for a sibling: new child, contacts created afresh.

## Tests (money and write safety: thorough; fakes only)
create once and `child_create_sent` blocks a replay; the Rejected/Unknown settle matrix; crash after create -> needs_review and a Retry finds the child with no second create; a child appearing before the write -> stop, no create, no booking; the booking payload equals the approved one with `childId` filled; child payload has NO `lastDay` and the right group by age at firstDay; contact order and independence; bill payer unconfirmed -> needs_review, never a second send; no names in Slack/log/outcomes; `new_child_not_built` gone and dry mode unchanged; URN taken from the shared allocator and not duplicated.

Out of scope here: a live test on a test deal (Drew approves and runs it later).
