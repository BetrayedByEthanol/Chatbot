# MVP-06B — Guided procedures

Architecture v2.5.1 §§6.3, 18.3–18.4, 21.3–21.4; confirmed PPA-US-053/054/055/056/100. Baseline `54ce3cb916398908b3cb7762330c167a58fabbca` after PR #127 technical CI (2,182 passed, 3 skipped) and live integration success. This is a bounded vertical slice, not a new Procedure Manager or a full Phase 3 gate.

## User interaction and authority

An existing, explicitly authorized ProcedureRun retains its immutable template version, optional private variant revision, rendered parameter values and ordered success tests. Natural guidance does not start a new run, publish a method, change a goal or authorize an agent operation. Initial run creation and final run confirmation retain existing typed owner commands.

Use "Guide me through procedure: Nginx self-signed TLS." or "Continue procedure: Nginx self-signed TLS." to identify a unique unfinished run by exact pinned title (or supply its run UUID to disambiguate). Unbound or ambiguous requests do not guess. Bare next/repeat/back/skip/clarify can use the immediately prior delivered guidance binding or an existing uniquely linked focused Dialogue thread. Bare pause/resume/cancel needs an adjacent binding; explicit named or procedure-specific requests abstain if unbound. Arbitrary informal conversation retains its existing model path. Explicit user step reports do not become evidence without a current delivered binding.

| Request | Result |
|---|---|
| Next / continue | Show first unresolved recorded step, not the next numbered step by assumption |
| Repeat | Show current unresolved instruction, or repeat the current review view |
| Back / previous step | Review earlier pinned step without undoing completed evidence |
| Clarify | Show pinned instruction and success test; no invented diagnosis or method change |
| Skip | Explain that a required step cannot be marked complete by skipping; retain the unresolved step |
| I tried this step. | ATTEMPTED (user report) |
| This step failed: [detail] / This step is blocked: [detail] | FAILED / BLOCKED with verbatim authenticated detail; do not infer cause, adapt method or advance |
| I completed this step. | USER_REPORTED_DONE; deliver the pinned success test |
| I confirm this step passed its check. | Separate USER_CONFIRMED (not VERIFIED); then guide the first unresolved step |
| Pause / resume / cancel | Exact delivered run-specific challenge and separate confirmation; only valid owner transitions |
| Agent step | Indicate that authorized agent execution is needed; no operation enqueued |

If a report predates the confirmation window or comes from another conversation, repeat the explicit completion report in the current delivered report context to append a fresh user-evidence event. It does not create independent verification or rewrite the old report. Contradictory claims against a recorded completed step remain gated by the existing explicit correction contract; no silent overwrites. When all steps are confirmed or verified, report that state; do not automatically finalize the run or complete an associated Task/Goal. PAUSED requires approved resume; NEEDS_REVIEW is not silently reset.

## Owner contracts and transaction boundaries

- Migration 0053 introduces scoped `procedure_guidance_responses`: response/turn, run, optional step, run/step version, binding kind and actual delivery binding time. This is a response association, not a second run state or an execution queue. Navigation request evidence is the original authenticated source turn. A generated response is not a delivered binding. The deferred response FK permits an atomic binding/response commit.
- `procedure_guidance_contracts.py` recognizes bounded complete inputs (including short navigation forms). `procedure_guidance.py` resolves exact title/run ID, prior delivered binding or existing focused link, and renders one pinned step/check. It never uses model text to infer a step outcome. The response commit reads current authoritative progress under a short run lock after the existing conversation/selection fences. Full/auto skips inference; forced-model diagnostics still call the model but the owner-rendered result replaces its claim. No lock spans inference.
- Delivery acknowledgment checks selection, current turn/revision, exact pinned instruction, run/step version and USER executor before recording the first INSTRUCTED transition using the existing ProcedureRun owner. Stale/late delivery can remain a historical Conversation delivery but cannot bind a newer run or modify progress. Repeat/review retains the current outcome; existing owner step history stays authoritative. Non-key conversation and response locks permit concurrent FK validation by typed owner commands.
- Reports and confirmations require the immediately preceding acknowledged binding, same authenticated scope/generation/conversation, no intervening delivered asynchronous follow-up, and matching run and step versions. Only the first unresolved USER step can accept a new report. Confirmations revalidate the exact delivered challenge and source report, separate confirmation turn and 24-hour source window. Status changes similarly revalidate exact run, version and displayed challenge. The internal approval adapter is not a public command flag; existing typed digest approval remains unchanged.
- `procedure_state._command_in_transaction` and extracted ProcedureRun owner apply functions reuse current source validation, selected generation, command receipts, events/outbox and transition invariants. Separate guidance receipt namespaces and the enclosing response/delivery transaction preserve atomicity and duplicate replay. No schema change to pinned definitions, no new scheduler, no task completion, no agent dispatch. A user report renewal appends a same-state evidence event through the existing owner event mechanism only in delivered REPORT context.

## Acceptance fixture

`tests/test_mvp06b_guided_procedures.py` publishes and explicitly starts a synthetic four-step STABLE Nginx local TLS procedure (certificate/key, configuration, configuration test/reload, HTTPS verification). It exercises instruction delivery, failed attempt, report without confirmation, interruption, a newer shared template version, fresh DB connections and named fresh conversation, report renewal, user confirmation, back/repeat/clarify, required-step skip, pause/resume/cancel and all remaining user confirmations. It tests stale/late delivery, cross-scope/selection generation, duplicate confirmation, same-title ambiguity, stale owner changes and agent-step abstention. No shell command, Nginx instance, certificate or real-world outcome is executed or verified; test testimony is synthetic. The fresh-connection/session test demonstrates persistence but is not a separate live host process restart or real-model acceptance.

```sh
python -m pytest -q tests/test_mvp06b_guided_procedures.py \
  tests/test_phase3_runs.py tests/test_mvp03b2_procedure_dialogue_context.py \
  tests/test_phase3_integrated_acceptance.py
```

Candidate static review only; PostgreSQL and live integration CI NOT RUN at authoring. Full regression and independent review remain required.

## Boundaries and next increment

One delivered response binding per step discussion, exact named recovery, adjacent evidence, 24-hour confirmation window and required-step skip refusal are conservative implementation decisions. Current ProcedureStep contracts contain no optional/conditional skip semantics; adding these requires explicit owner validation rather than inventing completion. General free-form navigation/outcome interpretation, richer explanations and failure diagnosis, automatic thread linkage in a new conversation, conversational run start/finalization, positive document-grounded synthesis and real-model long-gap review remain open. Existing read-only contextual procedure selector remains available for broader discussion. US-100 stays P; other story statuses unchanged. 06C — bounded decomposition and evidence follows technical regression; 06D strategy research/adaptation, P2-F, Phase 2/3 exit and deployment gates remain separate.
