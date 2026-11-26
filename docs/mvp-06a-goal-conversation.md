# MVP-06A — Approval-pending goals

Authority: Architecture v2.5.1 §§6.1, 21.1; confirmed PPA-US-046, PPA-US-098, existing roadmap 06A. Baseline `ad221c403918857cb0252cdb56c34420bab6378a` after PR #126 technical CI and v2 real-model automated benchmark success. Independent quality review and full Phase 2 acceptance remain open.

## Scope and interaction

This is a contract-first bounded primary-goal vertical slice. A clear explicit goal request (e.g. "Set a goal to walk three times a week" or "My goal is to eat fewer sweets") creates a private candidate, not an approved root. Ordinary desires, tentative or hypothetical requests, vague approval and compound outcomes do not create or activate a candidate. The existing intent and dialogue processing remains available for them. The grammar uses only persisted authenticated input, not untrusted model labels. A recognized measurable weekly activity is normalized to an exact recurring outcome and criterion; otherwise ask for an explicit criterion, or accept a bounded user-specified "success means" clause. No deadline is inferred; horizon OPEN, dates unset; user approves this explicitly displayed scope. Goal quality/feasibility and arbitrary date/evaluation window capture are not inferred.

Example:

1. User: Set a goal to walk three times a week.
2. Persona: Proposed goal: Walk 3 times per week. Success: Complete at least 3 walks per week. No end date is set. Reply 'Approve this goal.' to activate it or 'Don't save this goal.' to decline.
3. User: Change this goal to walk twice a week.
4. Persona displays revision 2 with exactly two walks per week and a fresh approval challenge.
5. User: Approve this goal.
6. Existing Goal owner creates one root at version 1; proposal revision 2 becomes APPROVED. No task is marked completed.

A broad goal is a draft with no criterion. "For this goal, success means at most two sweets per week." creates the next revision of an eating-sweets draft; the original objective remains unchanged. Missing criteria cannot be approved. A rejection terminates the current proposal; an intervening input, generic yes or new request supersedes the earlier challenge. A fresh explicit request can start again. No automatic split/merge of related objectives; separate requests remain separate goals, with links and broader semantics still open (US-098 criterion 3).

## Contracts, ownership and recovery

- Migration 0052: Goal-owned `goal_conversation_candidates` with stable goal ID, immutable payload per revision (1..12), private user/instance/conversation/generation scope, original source turn and verbatim source quote, revision proposal turn, generated response and sequence, outcome, optional success criterion, horizon, status and decision turn/event. One pending version per conversation. PENDING / SUPERSEDED / REJECTED / APPROVED are distinct from goal_records ACTIVE/PAUSED/ABANDONED. Deferred response FK allows atomic creation with generated response; no pending proposal is actionable as a Goal. The existing owner stream/outbox publishes candidate transitions.
- Goal candidate owner `goal_conversation.py` prepares/updates only from persisted authenticated input under Conversation's current-turn and selection lock. The user must have actually received the exact canonical proposal in the same conversation and generation before the immediately next explicit confirmation; verify response text, delivery, no intervening delivered asynchronous follow-up, latest turn and one-day bound on source. Approval of an undelivered, expired, changed, rejected, switched or nonadjacent candidate cannot activate. An incomplete draft cannot activate. A pending record may remain historically PENDING until superseding input, but the read-only scoped `list_goal_candidates` exposes approval availability separately; generation or expiry removes availability. No silent auto-resume after interruption. Same-conversation restart can replay the original durable response and approve if still eligible.
- Shared Goal owner `_command_in_transaction` and `_create_goal_apply` reuse existing creation/selection/source checks, hierarchy validation, P0 command receipt, goal.created event and outbox. Separate internal conversational receipt namespace prevents typed command receipt collisions. The natural-language approval verifier reconstructs the exact CreateGoalCommand from the delivered candidate and approval turn; rejects non-root and any payload mismatch. Existing public create_goal digest approval remains unchanged for non-conversational goals and cannot bypass a conversation candidate ID. Candidate transition, Goal creation and Conversation response are one transaction; duplicates replay the response. No LLM or external call inside transaction. Non-key locks on the conversation/turn match FK-compatible entity write path.
- Auto response orchestration bypasses model inference for the narrow grammar; forced-model diagnostics still invoke the gateway but cannot replace the canonical owner result. No new model pass or worker. Goal approval is not tool permission, a completion claim, strategy selection or autonomous work before approval. Existing Goal/Task/Operation authorization remains authoritative.
- Limits: 24-hour adjacent approval and twelve proposal versions are conservative implementation policies, not permanent architecture requirements. Unrelated or ambiguous messages retire rather than silently bind. General free-form correction, named cross-session resumption, explicit date/evaluation-window dialogue, multiple related goal links, unsolicited agent proposals and wider naturalness remain open. No arbitrary objective or success evidence is invented from the model. US-098 remains P, US-046 I unchanged; no new model selection or Phase 3 exit.

## Verification

New `tests/test_mvp06a_goal_conversation.py` exercises bounded parsing, source and revision history, success clarification, rejection, missing criterion, adjacent delivered approval, no preapproval work, exact typed-owner boundary, duplicate/concurrent approvals, stale input, scope, switch, expiry and model-output enforcement with fresh PostgreSQL connections. Existing `tests/test_phase3_goals.py` and cross-domain regressions must remain green. Candidate CI NOT RUN at authoring; no independent real-model acceptance claimed.

```sh
python -m pytest -q tests/test_mvp06a_goal_conversation.py tests/test_phase3_goals.py
```

Focused 06B — guided procedures after successful technical CI; Phase 3 gate still requires Phase 2 exit and subsequent 06C/D work. Target-model quality, remote deployment and applicable privacy lifecycle gates unchanged.
