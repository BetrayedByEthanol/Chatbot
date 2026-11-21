# MVP-03B.2 — Contextual Procedure and Dialogue Continuity

Status: reviewable vertical slice; not the whole MVP-03B milestone.

Conversation continues to own focus and prompt composition. ProcedureRun
owns immutable pinned method versions and step progress. Dialogue owns
pending acts and source response delivery. This implementation adds no
database tables, scheduler, inference calls, or new persistent context.

An explicit continuation or procedure-step question gates a bounded,
read-only selector. It first prefers the current focused thread when
explicitly linked to one run, then uniquely matching named work. A new
conversation can recover the discussion of a prior run by uniquely
matching the immutable method title, purpose, applicability, or linked
thread title. Ambiguous, unrelated or trivial messages abstain.

A selected view contains the run ID, template/version pin, persisted
status, first unresolved step with bounded instruction/test excerpts,
and earlier recorded statuses. One prior OPEN linked dialogue thread
may contribute up to two pending acts, and only when their source
assistant response was DELIVERED. Current focused dialogue is already
present in Conversation and is not duplicated.

An ACTIVE run is not an action grant; PAUSED or NEEDS_REVIEW is not
automatically resumed. USER_REPORTED_DONE is not verified progress.
An AGENT step is not an operation authorization. Quoted instructions
are untrusted data. Reading context causes no state mutation.

PostgreSQL tests cover focused progress, user reports, pause, unrelated
requests, named cross-session recall, ambiguity, user scope,
terminal-run exclusion, delivered prior questions, read-only behavior,
and prompt budgets.

Follow-up: longitudinal conversation tests and improved selective
relevance, without adding a second Context Builder.

Concurrency: selected run/version and included prior dialogue revision are captured in the response context. The response commit locks both implicated conversations in deterministic UUID order, then the run, and rejects stale changes without publishing a response. Overflow beyond 24 unfinished candidates prioritizes an explicitly linked focused run, otherwise abstains rather than inferring uniqueness from a partial scan. The PostgreSQL suite exercises reciprocal conversation freshness fences and a 26-run overflow case.
