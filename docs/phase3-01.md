# Phase 3 — Goals & Procedures: implementation roadmap

Architecture authority: **Persistent Persona Agent Architecture v2.5**, §§17.2, 18.3, 21.1–21.4, 22, and the **Phase 3 exit gate** in §14. The authoritative hierarchy remains:

**Goal → Subgoal → Strategy → Procedure → Task → Operation**

A strategy is an alternative approach to a goal. A procedure is a reusable, versioned method. A task is actionable work for a person or agent; an operation is an authorized, scheduled unit of execution. Dialogue progress and procedure-run progress must remain distinct.

## Phase 3 increments

| Milestone | Smallest useful contract | Acceptance gate |
|---|---|---|
| **P3-01 — Goal & Strategy Foundations** | Approved outcome hierarchy, bounded subgoals, strategy candidates and explicit selection, user-reported progress evidence | Persist/recover approvals, no inferred success, bounded concurrent tree updates, a failing approach cannot silently replace the objective |
| P3-02 — Actionability & Task Dependencies | Concrete user/agent tasks, evidence-based completion predicates, prerequisites, priority and dependency DAG | Bounded lazy decomposition reaches verifiable tasks, cycles and false completion rejected |
| P3-03 — Procedure Library & Versioning | STABLE, PARAMETERIZED and ADAPTIVE templates; immutable definition versions; shared vs personalized scope; input validation | Existing stable steps are not rewritten after failure; unapproved shared updates never affect private variants |
| P3-04 — Procedure Runs & Conversation Progress | Durable step state NOT_STARTED → INSTRUCTED → ATTEMPTED / USER_REPORTED_DONE → VERIFIED or FAILED/BLOCKED; step provenance, dialogue link | User can resume SSL setup after interruption/restart without fabricated step completion |
| P3-05 — Outcome Evaluation & Safe Adaptation | Goal/procedure outcome reviews, evidence sufficiency, strategy selection suitability, bounded parameter tuning, research-candidate authority | Failed method can be adapted after evidence and authorization; main goal remains unchanged |
| P3-06 — Phase 3 Integrated Acceptance | Combined 50-turn procedure, dependent verifiable tasks, concurrent P0 operations, cross-persona fencing, retention, latency | **v2.5 §14 Phase 3 exit:** recursive plan reaches verifiable tasks and review adapts a failing method without displacing approved objective |

Milestones are proposed implementation increments, **not changes to the seven-phase roadmap**. This incremental sequence retains P0 operation ownership and P2 memories, emotion and cognition.

## P3-01 scope and invariants

Migration: `0014_goals_strategies.sql`; owner: `goal_state.py`; typed commands and read models: `goal_contracts.py`.

1. **Action-bound authorization:** a source quote proves origin, not consent. Root/subgoal creation, status changes and strategy selections require a separate authenticated user turn containing exactly `goal_action_approval_phrase(command)`. The SHA-256-derived challenge is bound to the full proposed action, its target and expected version, and its original source. The confirmation must follow the source within 24 hours and is recorded in the domain event. A negated/ambiguous quote or generic "yes" is not an approval. The Conversation Manager/UI must present the exact proposal before requesting confirmation; the token contract is a narrow P3-01 approval boundary, **not a complete conversational confirmation UX**. Non-executing strategy proposals and unverified user reports do not require the challenge.
2. **Approved goals are outcomes**, with separately stored `desired_outcome`, `success_criterion`, `horizon`, optional target and evaluation window, selected-persona scope, privacy=`INTERNAL`, source and status. Root goals start `ACTIVE`. Status transitions permit pause, resume and abandon; there is **no automatic `ACHIEVED` status** until the later evaluation domain defines sufficient verifiable completion evidence.
3. **Lazy bounded decomposition:** subgoals point to the owning parent and start at depth 0 (root). Maximum depth 5 and 12 immediate children are provisional safety caps. Parent goal row and version are checked and incremented atomically. Parent-to-child edges are acyclic because an immutable parent must already exist at creation. No automatic recursive expansion or model-inferred goals.
4. **Strategies are immutable candidates** tied to a specific scoped goal. Proposing one neither changes its goal nor selects the method. `select_goal_strategy` requires an explicit authenticated command and expected goal version, records an append-only selection event and preserves the goal's objective and success criterion. A reported obstacle cannot silently switch strategies. No approach is automatically considered suitable or safe because it is proposed.
5. **Goal evidence is not verification:** `UNCLASSIFIED`, `PROGRESS`, `OBSTACLE` and `SUCCESS_REPORTED` are source-linked *candidate classifications*. Entries carry `USER_REPORTED` provenance and `classification_status=UNREVIEWED`; later evaluation must not promote labels directly to verified outcomes. A mistaken polarity, agent instruction or cognitive hypothesis cannot establish task or goal success. Duplicate same-turn reports cannot increase progress repeatedly.
6. **Selective serialization and recovery:** only conflicting changes to the same goal are serialized with `FOR UPDATE`; read-only views and independent goal changes are separate. Each command has P0 scope/generation authority, per-command idempotency receipt, version check and transactionally published goal-domain event/outbox. Duplicate commands replay even after persona switching; new commands require current selected Foreground authority. No LLM calls or network requests inside transactions.
7. **Selected-persona read isolation:** persona-facing goal, strategy and evidence reads require the currently selected instance. A user-authorized cross-persona export would need a separate audited platform interface. No additional foreground inference or context injection is introduced. The Conversation Manager can retrieve its selected persona's goal tree when relevant.

### Known boundaries of this slice

P3-01 does **not** implement task scheduling, dependency DAGs, user goals marked VERIFIED/ACHIEVED, procedure templates, parameter validation, execution steps, autonomous strategy research, learned tuning or response delivery. The `USER_REPORTED` evidence and `INTERNAL` goal data are never shared across users or silently promoted to common recommendations. Status changes to parent goals do not automatically mutate child statuses; future task eligibility must consider ancestor state and user priority.

The P3-01 API is an **authorized command surface**, not natural-language intent detection. The caller presents the full proposed action and records its separately authenticated exact confirmation before invoking the domain. Natural-language approvals such as "okay" need an explicit pending-dialogue approval mechanism before they can replace this provisional challenge. Consequential or health-sensitive strategies still require independent suitability review in P3-05.

## Validation

`tests/test_phase3_goals.py` exercises actual PostgreSQL state across fresh connections: action-bound approval/replay, tampered and negated confirmations, cross-persona read isolation, hierarchical CAS under concurrency, depth/fan-out budgets, unreviewed evidence categorization, candidate vs selected strategy, strategy switching without goal mutation, pause/resume/abandon, version errors and persona switching. The existing full PostgreSQL and live Ollama CI remain required.

**Phase 3 cannot be marked complete until P3-02 through P3-06 are implemented, tested and reviewed.**
