# MVP-07A — Authenticated local text application boundary

Architecture v2.5.1 §§8.2, 11.3, 14; US-004/009/010. Baseline `1cf551755e0478bff19bf2efde1199a490ecf634` after PR #130, 2,261 PostgreSQL tests passed and live integration passed. This candidate implements a bounded operator-provisioned **local** text application, not a public network API or production identity provider.

## Trust boundary

- Existing `acceptance_host` remains an operator-owned synthetic acceptance harness. Do not expose it or the Python domain functions to untrusted callers.
- `AuthenticatedTextApplication` is a separate strict JSON-bytes entrypoint; `serve_jsonl` is a bounded confidential local binary pipe adapter. No listener, framework, login/password endpoint or arbitrary RPC. The local operator owns the process, DB connection, provider configuration, account verification, credential issuance/revocation and confidential transport. A remote deployment requires additional design and acceptance.
- A trusted operator (server-constructed `TrustedPlatformActor` for the verified user, never deserialized client JSON) calls `issue_application_session` on an idle autocommit connection for the selected Foreground instance and generation. The function generates a 256-bit random bearer, stores only its SHA-256 digest, pins server-side user/instance/conversation/generation, records issuer and expiry, returns secret once. An optional existing conversation must belong to that scope. Default lifetime 1h, allowed 1s–24h; credential lifetime is an operational security bound, not a personal data retention policy. The operator distributes it confidentially; no credential in CLI args or logs. `revoke_application_session` requires trusted operator scope, is idempotent and records revocation metadata. No automatic persona switch or task cancellation.
- Each request authenticates the bearer and reconstructs its server-side `TrustedActor` with session provenance. Client cannot send actors, user/persona IDs, source IDs, generations, worker claims, tool configs, capability grants, callbacks, provider URLs or model identifiers. The optional application session correlation on the internal actor is not a public authentication credential. Existing trusted local actors remain compatible.

## Transport

```sh
# Operator supplies DB/model configuration through the process environment.
python -m persona_agent.application_host
```

Required `PERSONA_APPLICATION_DSN`, `PERSONA_APPLICATION_MODEL`; optional operator-only `PERSONA_APPLICATION_MODEL_URL` (defaults to loopback Ollama). Use only a trusted local provider for this slice. No real user transcripts to benchmark hooks or unreviewed external providers. Use a confidential local pipe, not a public socket. The operator must not log raw requests or response bodies; the adapter emits sanitized errors and does not echo the secret.

Each JSONL line is an envelope with token and request. The examples below use a placeholder, not a usable credential.

```json
{"token":"<operator-issued bearer>","request":{"action":"message","text":"Hello","idempotency_key":"m1"}}
{"token":"<operator-issued bearer>","request":{"action":"status"}}
{"token":"<operator-issued bearer>","request":{"action":"resume","turn_id":"<turn UUID>"}}
{"token":"<operator-issued bearer>","request":{"action":"ack","response_id":"<response UUID>"}}
{"token":"<operator-issued bearer>","request":{"action":"presence","idempotency_key":"p1"}}
```

Message persists authenticated ingress and uses the existing response orchestrator. Fixed-length idempotency keys are domain-separated by conversation/generation/action so reconnect/renewal in the same scope preserves retry identity. Resume is a scoped retry of a known turn; pending input may need inference; a committed response replays without new inference. Status gives latest turn/response metadata plus up to 32 scoped existing conversation-tool operation statuses, not raw tool payloads/results/leases/errors. ACK requires the exact durable response offer for the presenting session, not a guessed ID. The client must ACK only after accepting the response; a client assertion is not independent proof of perception. Presence is an explicit authenticated client action using the existing lifecycle owner; this slice does not reawaken Background or accept forged departure/switch/platform callbacks. Client reconnect is not itself proof of presence.

## Transaction and revocation rules

Migration 0056 adds scoped sessions and delivery offers, with a composite FK to the response's exact user/instance/conversation/generation. The session row records issuer, expiry and revoker; no raw credential retained. Application actor selection checks live authority, holding a short session SHARE lock after the existing selection lock. Ingress (including idempotent replay), response context/commit, replay, offer, ACK and authenticated presence fence scope, current generation and session. Revocation serializes against short commits; no session/selection lock spans model generation or external transport. Revocation during inference prevents subsequent response commit, but cannot undo already committed input, recall bytes already emitted or cancel an in-progress provider request. A successful offer is not an ACK. A revoked/expired or switched session cannot replay or mark a response delivered. Superseded GENERATED replies cannot be offered or acknowledged; already delivered same-scope replay remains idempotent.

No asynchronous tool admission or cancellation is enabled for application actors. Explicit text-analysis requests are denied, not converted into privileged requests; scheduler/tool entrypoints reject application admission even if called directly from trusted code. Registered immediate clock and existing conversation/owner paths remain available, subject to existing evidence/approval checks. Provider choice and model outputs do not grant any authority. Existing independently authorized platform/worker operations remain governed by their own P0 fences and are not cancelled by session revocation.

## Verification and limits

`tests/test_mvp07a_application.py` exercises strict envelope fields, tenant and same-user conversation scope, selected persona/generation, expired/revoked credential, replay/renewal and exact offer ACK, superseded response, presence retry, revocation during inference, denial of asynchronous admission and model authority, scoped operation-status metadata and bounded sanitized pipe parsing. Existing Phase 0/1/3/4 regression is mandatory.

Candidate static review only; PostgreSQL and live CI NOT RUN at authoring. Local transport is deliberately not a public deployment: real user account enrollment/authentication, credential distribution/storage, rate limits, secure remote transport, authenticated presence/disconnect lifecycle, per-capability async dispatch and revocation during external effects, provider data policy, comprehensive delivery-time source invalidation, and 07B retention/erasure remain open. No guarantee of network security, immediate remote provider cancellation, revocation of already-delivered content or end-to-end deletion. US-004/009/010 remain P. Remote/untrusted deployment remains blocked; proceed with 07B owner-specific lineage and policy decisions.
