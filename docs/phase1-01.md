# Phase 1-01 — Durable text conversation vertical slice

**Status:** First implementation slice of Phase 1 — Responsive Core / MVP.
It is not the full Phase 1 acceptance gate.

## Authority and persistence

Architecture v2.5 Sections 4, 14, 18 and 23 require immediate authenticated
input persistence, selective bounded context, model inference outside locks,
optimistic dialogue updates, and response-delivery state distinct from
generation. This slice adds migration 0005 and three small modules:

- conversation_contracts.py — typed user input, prompt, model and output
  contracts; no global mutable conversation state
- conversation.py — authenticated text ingress, durable idempotency receipt,
  conversation revision, recent context selection, stale-response fencing,
  response journal/outbox and explicit delivered acknowledgment
- model_gateway.py — dependency-injected Ollama HTTP chat gateway using
  standard-library networking with bounded timeout and response validation

Conversation identity is scoped to (user_id, instance_id, conversation_id).
The active durable state contains revision, next sequence, last input turn
and summary watermark. Full dialogue thread/topic and procedure state are
future slices, not fabricated in this one.

## Transaction boundaries

1. A trusted ingress adapter supplies TrustedActor and TextInputCommand.
   ingest_text commits the user text, receipt, event and outbox atomically.
   A duplicate command returns its original receipt without activating the
   old persona. A new command requires the selected Foreground generation.
2. prepare_text_context loads recent verbatim messages and minimal persona
   identity within a character/turn budget. It includes only DELIVERED
   assistant replies. No mandatory extra classifier or long-term retrieval.
3. respond_to_text calls the injected gateway outside any database
   transaction and passes the output to commit_text_response. A newer user
   input, competing response or selection switch prevents a stale commit.
4. The reply is GENERATED until the output adapter actually delivers it.
   acknowledge_text_delivery records DELIVERED and appends a matching event.
   Model timeouts leave original user inputs PENDING for retry after restart.

All mutation APIs require idle autocommit psycopg connections and hold only
short PostgreSQL transactions. Model/tool/provider calls never run under DB
locks. Sensitive raw user input stays in the scoped conversation table,
not duplicated into event payloads.

## Reference usage

    with psycopg.connect(dsn, autocommit=True) as conn:
        receipt = ingest_text(conn, actor, TextInputCommand(
            conversation_id=conversation_id,
            instance_id=selected_instance_id,
            expected_generation=generation,
            idempotency_key=stable_client_message_id,
            text=user_text,
        ))

    gateway = OllamaChatGateway(model="lexi")
    with psycopg.connect(dsn, autocommit=True) as conn:
        generated = respond_to_text(conn, actor, receipt.turn_id, gateway)

    # Only after the output channel actually delivered generated.text:
    with psycopg.connect(dsn, autocommit=True) as conn:
        acknowledge_text_delivery(conn, actor, generated.response_id)

The hosting application must provide authenticated actors and select the
appropriate model endpoint; no HTTP ingress or production model deployment
is asserted by this slice. A generated response is not evidence of playback.

## Tests and continuation

The new real-PostgreSQL suite verifies duplicate ingress, restart and
context continuation, cross-user and persona isolation, model timeouts,
concurrent reply commits, stale generation after a switch, response-delivery
fencing and context budgets. Model-gateway tests stub network calls.

The v2.5 Phase 1 exit gate remains incomplete. Follow-on increments:

- P1-02 — a typed and authorized time tool, slow tool, await/defer decisions,
  durable operation/turn correlation and relevance-checked continuation
- P1-03 — minimal durable dialogue threads/pending acts, channel-aware
  response state and measured latency/health metrics

Phase 2 retains ownership of extended multi-tier memory, emotional inference
and asynchronous cognitive reflection.
