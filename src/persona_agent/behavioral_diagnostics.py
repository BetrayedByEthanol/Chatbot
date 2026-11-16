"""MVP-01F.1: observational diagnostics for the already executed model pass.

Never performs inference, persists intent, or authorizes domain changes.
The Ollama gateway still owns response parsing and validation.
"""
import json
from pydantic import ValidationError

from .conversation_contracts import ModelIntentCandidate
from .intent_interpretation import diagnose_model_candidate_gate


def describe_interpretation(raw: str | None, candidate=None) -> dict:
    """Explain absence of a candidate without guessing model intent."""
    if raw is None:
        return {"outcome":"RAW_RESPONSE_UNAVAILABLE", "candidate":None}
    record = {"raw_response":raw, "candidate":None}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        record["outcome"] = (
            "INVALID_STRUCTURED_JSON" if raw.startswith(("{", "["))
            else "PLAIN_RESPONSE_FALLBACK"
        )
        return record
    if not isinstance(payload, dict):
        record["outcome"]="NON_OBJECT_RESPONSE"
        return record
    source = payload.get("intent")
    if source is None:
        record["outcome"]="INTENT_NULL"
        return record
    if not isinstance(source, dict):
        record["outcome"]="INTENT_NOT_OBJECT"
        return record
    try:
        validated = ModelIntentCandidate.model_validate(source)
    except ValidationError as exc:
        record["outcome"]="INTENT_SCHEMA_REJECTED"
        record["validation_errors"] = [
            {"field":".".join(map(str,e["loc"])), "type":e["type"]}
            for e in exc.errors()
        ]
        return record
    record["candidate"]=validated.model_dump(mode="json")
    record["outcome"]="VALID_CANDIDATE"
    # This is a sanity check: a normal gateway must return the same candidate.
    if candidate is not None and candidate != validated:
        record["outcome"]="GATEWAY_CANDIDATE_MISMATCH"
    return record


def resolve_interpretation(
    record: dict, *, input_text: str, turn_id: str, snapshot: dict,
) -> None:
    """Add source-grounding and the exact persisted-turn outcome.

    The runtime and this observer share the pure pre-admission gate; a
    hypothetical model proposal is never presented as an approved goal.
    """
    signals = snapshot["database"]["conversation_intent_signals"]["rows"]
    turn_signals = [x for x in signals if str(x["turn_id"])==turn_id]
    rows = snapshot["database"]["conversation_turns"]["rows"]
    turn = next((row for row in rows if str(row["turn_id"])==turn_id),None)
    if turn is None:
        record["admission"]="TURN_NOT_IN_BOUNDED_SNAPSHOT"
        return
    fallback = next((
        x for x in turn_signals if
        x.get("method_revision")=="mvp01f2.routine_fallback.1"
        and x.get("epistemic_status")=="RULE_INFERRED"
    ),None)
    if fallback is not None:
        record["rule_fallback"]={
            "signal_id":str(fallback["signal_id"]),
            "method_revision":fallback["method_revision"],
            "intent_kind":fallback["intent_kind"],
        }
    candidate_data=record.get("candidate")
    if candidate_data is None:
        record["admission"]=(
            "RULE_FALLBACK_PERSISTED" if fallback is not None
            else "NO_VALID_MODEL_CANDIDATE"
        )
        return
    candidate=ModelIntentCandidate.model_validate(candidate_data)
    reason=diagnose_model_candidate_gate(
        input_text,turn.get("source_input_event_id"),candidate,
    )
    record["gate_reason"]=reason
    if reason!="VALID":
        record["admission"]=(
            "GATE_REJECTED_RULE_FALLBACK_PERSISTED" if fallback is not None
            else "GATE_REJECTED"
        )
    elif any(x.get("source_response_id") is not None for x in turn_signals):
        record["admission"]="MODEL_SIGNAL_PERSISTED"
    elif turn_signals:
        record["admission"]="PREEXISTING_SIGNAL"
    else:
        record["admission"]="VALID_BUT_NOT_PERSISTED"
