# MVP-07B2.8 — Emotion and Relationship evidence reappraisal (F03/F04)

Initial implementation on merged 07B2.7. Existing PostgreSQL owner projections
and immutable evidence remain authoritative. No vector store, cache service,
new model pass or scheduler.

- Emotion: retain historical applied flags and source-order watermark; replay
  only currently eligible applied contributions when the Memory scope revision
  or expiry changes. Read-time replay is nonmutating; a later applied cue
  persists the reappraised projection with its eligibility watermark. A newer
  ineligible user-affect cue is masked rather than falling back to an older
  feeling. Four-hour label expiry and eight-hour mood decay remain separate.
- Relationship: factual interaction count and familiarity survive withdrawal of
  content. Rapport is recomputed from still eligible originally applicable
  feedback, in event-stream order. Invalid pending feedback is neutral and
  does not admit a preference. Effective preferences use only the latest
  eligible stored revision, never an older value after invalidation.
- Explicit preference commands retain idempotency and expected-version checks,
  and reject ineligible new evidence. Owner locks precede the scope fence.
- Foreground presentation uses a separately tracked scope/expiry fence,
  persisted through the existing broad response lineage. Presentation-only
  replies can remain in recent verbatim context while current, inheriting both
  raw and presentation fences. General historical content and summary/cognition
  remain conservative. Cross-conversation dependencies are not fabricated as
  local raw IDs. Direct preference recall retains its exact revision fence,
  now eligibility-aware; historical planning/emoji questions also use the
  historical fence.
- Candidate queries are scoped before eligibility. Normal mood reads use the
  existing persisted projection when its watermark is current. Foreground
  stale replays are capped at 128 appraisal rows, otherwise optional mood is
  omitted until the owner next applies a cue. No unbounded foreground replay.

## Limitations and review gates

This is an initial implementation, not consolidated F03/F04 acceptance.
Owner projections are repaired lazily on the next applicable owner event;
there is no idle reappraisal job. Explicit owner snapshot reads can replay
the full scoped history; measure this on long-lived personas before adding
a dedicated durable reappraisal operation. A current presentation-only reply is reusable in recent history, but
dialogue-derived broad context and background derived-text readers retain
the prior conservative omission policy. Broad scope revisions can still
invalidate presentation replies due to unrelated changes within the same
persona; verify conversational continuity and latency before acceptance. No semantic correction
classifier, model-affect policy change, or cross-domain notification propagation
is added. Existing pre-0063 assistant replies without affect provenance cannot
be assigned invented dependencies. Review commit/delivery lock ordering and
exercise real PostgreSQL and full-suite scaling before merge.

## Initial regressions

The new test module covers correction/retraction/expiry, independent applied
mood/feedback survival, immutable observation history, late ineligible events,
preference revision non-resurrection, controlled cross-session response commit
and delivery, and time-only projection watermark expiration. Direct preference
recall now reads only requested global slots, not the full rapport projection.
Foreground topic-boundary selection is capped before source eligibility;
an ineligible candidate can leave an unused slot rather than scan all history.

Acceptance remains pending real PostgreSQL CI and review. General semantic
correction detection and proactive/notification consumers remain out of scope.
