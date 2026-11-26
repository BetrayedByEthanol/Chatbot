# MVP-07B1 — Initial data lifecycle and provenance inventory

Baseline PR #130 plus candidate 0056. Architecture v2.5.1 §§5, 12, 19.3 and US-117/118/085; DEC-03 remains open. This inventory groups the migration/owner surfaces of the current modular monolith and identifies copies requiring a policy. It is not a live production deployment audit, exhaustive column-level lineage graph, deletion implementation or compliance certification. No retention period, consent or jurisdiction is inferred.

## Current development boundary

Existing `compose.e2e.yaml` binds PostgreSQL to loopback; `compose.terminal-review.yaml` explicitly provisions isolated short-lived review DBs with loopback port and local password. Existing local acceptance host is operator-only; candidate 07A is a local credential-bound JSONL adapter. Ollama provider requests may include personal conversation/context; server configuration is trusted, but a production provider/retention policy has not been selected. Do not enable external or untrusted deployment on this evidence.

## Persisted owner groups and dependent copies

| Owner / migration range | Private records and dependent copies to inspect | Initial invalidation concern |
|---|---|---|
| Runtime, Event and Operation (0001–0004; 0021–0028; 0042; 0056) | user/instance/selection/presence, events/outbox/receipts, leases, checkpoints, result refs, dependencies, background epochs and authorizations; credential digest and offers | Revocation fences; receipts/event payloads can retain historical data; don't delete shared FK history blindly |
| Conversation and Dialogue (0005–0007; 0032–0036; 0043–0044; 0052–0053) | verbatim turns, generated/delivered responses, followups/tool results, dialogue threads, intents, historical search projections, temporal anchors, goal/guidance evidence | Generated or replayed output may contain superseded private text; delivery-time dependency fence still needed |
| Memory (0008–0011; 0037; 0044–0051) | facts, preferences, meanings, candidates/revisions, evidence/support, summary/episode text, entity links/relations, pattern hypotheses, semantic components, knowledge documents/versions/passages, retrieval feedback | Existing current-source eligibility and historical fence are useful but incomplete; trace every source revision and index/cache copy |
| Cognition, Emotion, Relationship (0012–0013; 0023; relevant later semantic migrations) | reflections/intentions, appraisals, user preference and relationship observations, critical updates | Derived inference is not verification; invalidate source-linked projections rather than silently overwrite or retain disputed claims |
| Goal/Task/Procedure (0014–0020; 0024–0028; 0038–0042; 0052–0055) | approved objectives, evidence quotes, strategy candidates, task reports/corrections, run step history, private variant revisions, evaluation/adaptation, recurring slots and research pins | Domain-owned immutable history may include personal evidence; invalidated source must stop eligibility without fabricating a new outcome or changing approved goal |
| Notifications, speech, multimodal (0029–0031; ingress and output adapters) | notification requests, delivery/speech state, audio provenance, local audio inputs/outputs and provider buffers | Prevent post-revocation dispatch; account for file/provider copies; no assumed erasure from external devices |
| Shared procedure/template definitions and platform configuration (0001, 0004, 0016 and relevant grants) | shared reviewed versions, resource pools and operator/publisher grants | Distinguish shared assets from private variants; never export private experience or remove other users' authorized state |

This grouping is a starting point for the per-table/per-field owner inventory, including FK/event lineage and retained payload classification. Do not infer that every migration or table is fully covered by a single owner or that append-only tables are exempt from approved privacy obligations.

## Non-database copies

| Surface | Evidence / risk | Required operator decision |
|---|---|---|
| Local model/provider | `model_gateway.py` nonstreaming request context; inference observations and possible provider logging | Approved local or external endpoint, allowed sensitive context, logging and provider retention/erasure capability |
| Evaluation bundles | `target_model_benchmark.py`, behavioral/natural dialogue tooling; ZIPs contain private transcript, requests, snapshots, reviews and diagnostics; `artifacts/` gitignored | Synthetic-only default; explicit consent and access scope for real data; inventory exports and delete/revoke derived reviewer copies |
| App/CLI/worker diagnostics | stdout/stderr, errors, traces, command history, temporary buffers | No secrets/raw private payload in routine logs; decide diagnostic access and retention, distinguish sanitized audit metadata |
| Audio/adapters | ASR/TTS input/output, local files or external device/provider copies | Explicit capture/use/retention permissions; deletion capability for actual storage path |
| PostgreSQL backups/WAL/replicas | deployment-dependent, not evidenced as configured here | Backup encryption/access, restore-time revocation replay, deletion schedule and operator ownership |
| Source control/CI/artifact storage | local .env and artifacts ignored by .gitignore; CI test and benchmark artifacts | Do not upload credentials or private real-user transcripts. Inspect actual artifact retention and operator access before deployment |

## Next: 07B2 and 07B3

1. Confirm actual deployment scope and data processors, user/operator responsibilities, consent, sensitive-data route policy, access and retention/erasure requirements. Do not invent durations or jurisdiction.
2. Establish owner-specific lineage with source ID/revision/trust/privacy/expiry, current eligibility and version fence. Reuse existing memory_read_policy and deferred historical recall revision triggers; start with generated response source dependencies and delivery-time revalidation, including cached/replayed output. Treat withdrawn document strategy evidence, derived episode/pattern and cognition links explicitly; no generic trust elevation.
3. Define authenticated scoped correction/revocation commands and durable invalidation/outbox behavior. Prove late worker/response results cannot reintroduce revoked evidence; rebuild or invalidate indexes/caches. Plan end-to-end erasure separately for append-only history, FK graph, receipts, backups and exports. Audit without retaining erased private content.
4. Test tenant isolation, correction/expiry, superseded responses, provider copies, restoration and replay. No destructive changes to authoritative records until policy and owner contracts are reviewed.

07B1 initial inventory only. 07B2/3 remain open. Remote/untrusted deployment blocked by 07A and applicable 07B gates.
