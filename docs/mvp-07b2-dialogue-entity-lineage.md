# MVP-07B2.7 — Dialogue, intent and entity lineage

Closure increment following the six merged 07B2 slices. Preserve actual authenticated speech, stable thread IDs, historical resolutions, approved goals and owner records. Current projections are not new evidence or execution authority.

- Migration 0062 extends the exact raw/summary turn predicate to current entity revision, relation, endpoint and linked-identity eligibility. Current relation source survives alias additions; older aliases are conservatively omitted after a new revision. Generic historical search retains its existing stricter entity-owned exclusion. Canonical entity lookups revalidate the relation source and entity history validates edge/endpoint sources. Entity-grounded response events pin exact versions at replay/delivery, including owner-writing natural reports that cannot capture their own pre-commit revision. Do not copy entity-grounded assistant prose into raw/summary/cognitive history. Adjacent known/choice bindings reject invalidated sources without guessing identity; a stale choice still routes to canonical reassessment instead of a model fallback. Recognition reports inherit their event-pinned entity version even when no new revision is written.
- Current Dialogue views validate origin, referent, intent and delivered response sources. Model-derived intent inherits source response raw dependencies; explicit next actions pin their source turn and automatic/replaced hints pin their source response. Unknown legacy hint provenance is masked rather than guessed. Thread identity/history remains intact, including when a title is masked in the active read view. Stale pending labels do not drive automatic resolution; cancellation remains available. Preference-fenced or entity-grounded bodies are not independent dialogue label sources.
- The foreground context collector adds local source IDs to the existing bounded 128-turn raw response fence, including sources outside the retained message window. Broad historical response dependencies use the captured historical fence when available; otherwise abstain. No new model call or cross-service cache. Current user input remains verbatim. Cadence retrieval checks the current source of the latest linked correction without falling back to an older correction. Procedure optional dialogue reads use current projections and capture before selecting candidates.
- Explicit topic and intent matching use current eligible candidates. Historical metadata is never rewritten just because a source becomes ineligible.

## Boundaries

Read and response boundaries are guarded, but ingress/explicit dialogue mutation transactions still rely on existing conversation serialization, not a new exact cross-owner late-write fence. A concurrent invalidation may leave a historical link or resolution; effective reads must not reuse stale labels. Reassessment of resolution-state contributions, explicit action resolution concurrency, owner-specific affect/relationship and general tool/notification propagation remain open. Cross-session procedure context uses the existing broad scope fence. Legacy unbound response rows fail closed. No generalized semantic correction, inferred identity merge, deletion, or deployment. Query cost needs measurement. Regression tests cover current projection masking, original source dependencies outside the retained message window, explicit cancellation, linked objective invalidation, entity correction/retraction/privacy/expiry, alias continuity and report recognition. Local PostgreSQL unavailable; static review only, CI pending.


## Foreground query regression correction

Local PostgreSQL EXPLAIN on PR #141 measured the former focused-thread join at
3,093.90 ms / 122,300 shared buffer hits, with 868 signal eligibility evaluations.
Adding explicit conversation/user/instance scope reduced it to 29.10 ms / 1,036
hits; the unvalidated primary-key lookup was 0.024 ms (diagnostic only).
JIT on/off did not materially affect the original query. The test suite also
showed time-dependent failures under the excessive latency.

The unmerged migration 0062 now correlates origin eligibility to the candidate
thread, and application joins use scoped, non-flattenable LATERAL subqueries
where joining a general current projection could cause unrelated validation.
Correlated historical checks include the complete record scope and an OFFSET 0
barrier. The focus lookup requires an exact thread identity and scope before
validating lineage. No eligibility predicate, expiry rule, provenance check,
response fence or durable history has been removed. No cache or vector service
is introduced.

A PostgreSQL regression checks actual predicate evaluation in the focused
lookup under normal and hash-join-favoring planner settings, including unrelated
threads in the same conversation, another conversation and another user.
The new code still requires real PostgreSQL integration and EXPLAIN verification;
the earlier 13 focused tests passed locally before this correction. Full-suite
failures must be reassessed after the performance correction, not masked by
raising timeouts.
