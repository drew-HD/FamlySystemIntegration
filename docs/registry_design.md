# Children registry (branch children-registry)

Store: var/children_registry.sqlite3 (gitignored), code in actions/registry/. Row per Famly child: name, DOB, URNs, Famly child id, operations deal id(s), ad hoc deal id(s). A deal id or URN already tied to another child is refused and reported, never overwritten. Rules: CLAUDE.md lines 6-8.

## Stages
1. Store, backfill (GET only, dry run unless --confirm, run from prod with Drew's approval; must catch every child), registry-lookup, registry-stats.
2. Both workers check the registry after their normal checks (URN, deal id) and add a row after a confirmed create; same-child Slack question.
3. Ad hoc deal joins an operations child: book the sessions on the existing child (3a path), add the ad hoc deal id, Slack summary ("ask parents to book on Famly next time").
4. Operations deal joins an ad hoc child: see decisions below.
5. Live tests on test children.

## Drew's decisions (2026-11-25)
- Exact name (same tokens, any order) + same DOB: join automatically, no question.
- Similar name + same DOB: Slack question. Twins (same DOB, different first names) are checked too, so they get the question.
- Parent email: a backup signal only (contacts can have several emails, and the primary email can change). It can raise the question; it never joins on its own.
- Join by operations: move the start date to the deal's start; do NOT change the group; remove the Ad Hoc Only tag; do not change any ad hoc sessions already booked.
- Backfill: one run from prod is fine if it catches everyone; nightly refresh later.
- No HUBSPOT reference is added to an existing child (no children.update). The registry holds the Famly id, URNs and deal ids; onboarding writes famly_childid on the operations deal. The server finds children through the registry.

## Risks
- Moving the start date uses internal GraphQL that is only proven for the end date; needs a live probe on a test child (stage 5).
- Check whether the plan/commit path finds a child by its HUBSPOT reference (protected path).
- The registry can drift from Famly (merges or deletions in the UI), so lookups re-check the child id live.
