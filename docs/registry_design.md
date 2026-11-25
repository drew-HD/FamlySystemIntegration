# Children registry (branch children-registry)

Store: var/children_registry.sqlite3 (gitignored), code in actions/registry/. Row per Famly child: name, DOB, URNs, Famly child id, operations deal id(s), ad hoc deal id(s). A deal id or URN already tied to another child is refused and reported, never overwritten. Rules: CLAUDE.md lines 6-8.

## Stages
1. Store, backfill (GET only, dry run unless --confirm, run from prod with Drew's approval; must catch every child), registry-lookup, registry-stats.
2. Both workers check the registry after their normal checks (URN, deal id) and add a row after a confirmed create; same-child Slack question.
3. Ad hoc deal joins an operations child: book the sessions on the existing child (3a path), add the ad hoc deal id, Slack summary ("ask parents to book on Famly next time").
4. Operations deal joins an ad hoc child: see decisions below.
5. Live tests on test children.

## Stage 1 (built, fakes only, not run live)
- `actions/registry/store.py`: tables children (Famly id, name, name_key, dob, site_code, source), child_urns, child_deals (kind operations|adhoc|weekend_school; the kind follows the pipeline, `set_kind` changes it), child_emails. A URN or deal on another child is refused (`conflict`), never overwritten.
- `match.py`: compare() -> exact | similar | none. Drew's rules (2026-11-25): same DOB required; EXACT (join) = same words, or the shorter name (2+ words) wholly inside the longer (middle name in HubSpot `child_full_name`); SIMILAR (ask) = 2+ words match or same surname (twins); a close spelling counts as a matching word. `deal_child_name()` reads `child_full_name`, else first + last.
- `backfill.py`: Famly roster (all sites, past and future) + Operations, Ad Hoc and Weekend School deals (famly_childid, child_urn) + done onboard/ad hoc jobs + URN allocations (local stores opened read-only). A deal is linked only when all sources agree on one child.
- The registry holds ONLY children that are in Famly, with every deal that points to them (any stage). A deal with no Famly child is never stored; `--deals-out reports/x.txt` lists those deal ids (Left Nursery, Ad Hoc Completed, Closed Lost left out) to run through the pipeline later.
- Commands: `registry-backfill [--confirm] [--out reports/x.csv] [--deals-out reports/x.txt]`, `registry-lookup (--child-id|--urn|--deal-id|--email|--name N --dob D)` (names, ids, URNs and deal ids to the terminal; never DOB or emails), `registry-stats`.

## Stages 2 and 3, ad hoc worker (branch adhoc-registry; fakes only, not run live)
- Planner (`actions/adhoc_plan/planner.py match_child`): after the deal's own ids and the allocated URN, the registry by name + DOB (live roster drops stale ids). One EXACT = that child (wins over the parent-email hold); 2+ EXACT = `child_ambiguous`; SIMILAR = hold `registry_similar_child` -> the Slack child-match question; registry unreadable = hold `registry_unreadable` (Slack hold notice).
- A child with an operations deal in the registry: `adHocOnly` False (no Ad Hoc Only tag, end date kept); flags `joins_operations_child`, and `operations_child_no_famly_plan` when Famly has no plan.
- Worker (live only, never a stop): a new child's registry row after the confirmed create; an existing child's row (from the live roster) and the ad hoc deal after the booking. Outcome `registry:added|already|conflict|missing|failed`.
- Reporting: Slack done message (operations child, "book on Famly next time", no-plan flag with child name and deal id, registry result, HubSpot failures marked "Needs checking"). HubSpot (only when there is something to follow up; no notes on deals, Drew 2026-11-25): the dropdown `famly_followup` (no_famly_plan | registry_conflict | registry_failed) on each operations deal. Drew creates `famly_followup` in HubSpot before going live.

- `famly_plan_status` on the AD HOC deal (branch adhoc-plan-status): after a booking attempt only. Booked cleanly = `plan_created` (note cleared); booking sent but needs a person = `needs_attention` (note = codes). Holds, rejected bookings and dry mode write nothing; a failed write is a Slack line.

## Drew's decisions (2026-11-25)
- Exact name (same words any order, or the Famly name wholly inside the HubSpot name, e.g. a middle name) + same DOB: join automatically, no question.
- Similar name (2+ words match, or same surname: twins) + same DOB: Slack question. One shared first name alone is not similar.
- Parent email: a backup signal only (contacts can have several emails, and the primary email can change). It can raise the question; it never joins on its own.
- Join by operations: move the start date to the deal's start; do NOT change the group; remove the Ad Hoc Only tag; do not change any ad hoc sessions already booked.
- Backfill: one run from prod is fine if it catches everyone; nightly refresh later.
- No HUBSPOT reference is added to an existing child (no children.update). The registry holds the Famly id, URNs and deal ids; onboarding writes famly_childid on the operations deal. The server finds children through the registry.

- Historical ad hoc and holiday camp deals held in the Operations pipeline were re-typed once to `adhoc` from `service_of_interest__1_` (2026-11-25, one-off; not in the code: new deals sit in the right pipeline).

## Risks
- Moving the start date uses internal GraphQL that is only proven for the end date; needs a live probe on a test child (stage 5).
- Check whether the plan/commit path finds a child by its HUBSPOT reference (protected path).
- The registry can drift from Famly (merges or deletions in the UI), so lookups re-check the child id live.
