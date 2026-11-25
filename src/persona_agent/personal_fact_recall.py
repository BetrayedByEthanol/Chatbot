"""MVP-04C.3: canonical replies for a narrow saved personal-fact route.

The Memory owner supplies a scoped record and its actual source. Conversation
renders the answer in the existing response commit, not from model prose or
an earlier prompt snapshot. No raw historical retrieval, Memory writes,
external verification, additional inference, or new authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID

from .response_decisions import personal_fact_topic

ROUTE_ID = "conversation.personal_fact_recall.v1"
POLICY_REVISION = "mvp04c3.1"
Status = Literal["KNOWN", "UNKNOWN", "DISPUTED", "UNSUPPORTED_SOURCE"]
_VERBS = {"car": "drive", "phone": "use", "camera": "own"}
# Conservative presentation, not a new privacy admission policy. Only a
# short model/name-like value of an already eligible NORMAL Memory slot.
_VALUE = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,79}")
_QUALIFIED = re.compile(
    r"\b(?:not|no|never|maybe|perhaps|probably|might|could|would|"
    r"used to|sometimes|previously|borrowed|borrow|rented|rent|"
    r"or|and|but|unless|if|password|secret|token|pin|number|account|"
    r"address|serial|license|registration|joke|example|hypothetical)\b", re.I,
)


@dataclass(frozen=True)
class PersonalFactAnswer:
    text: str
    status: Status
    memory_id: UUID | None = None
    version: int | None = None
    source_turn_id: UUID | None = None


def _normalized(value: str) -> str:
    return value.strip().replace("’", "'").casefold()


def source_supports_value(topic: str, value: str, source: str, quote: str) -> bool:
    """Require the entire first-party source to entail the saved relation.

    A substring quote alone cannot turn a question, negation, historical
    report or hypothetical into evidence of a current personal fact.
    The exact topic identity is supplied by the Memory-owned slot; the
    supported relation is checked independently against the source.
    """
    verb = _VERBS.get(topic)
    if (verb is None or not _VALUE.fullmatch(value) or _QUALIFIED.search(value)
            or not 1 <= len(source) <= 240
            or _normalized(quote) not in _normalized(source)
            or not quote.strip()):
        return False
    pattern = (
        r"(?:actually,?\s+)?i\s+(?:now\s+)?" + verb +
        r"\s+(?:(?:a|an|the|my)\s+)?" + re.escape(value) +
        r"(?:\s+" + re.escape(topic) + r")?[.!]?"
    )
    return re.fullmatch(pattern, source.strip().replace("’", "'"), re.I) is not None


def render_saved_fact(topic: str, row: dict | None) -> PersonalFactAnswer:
    """Do not reveal a value from an unavailable or disputed record."""
    if topic not in _VERBS:
        raise ValueError("unsupported personal-fact relation")
    unknown = "I don't have a reliable saved answer for that."
    if row is None or not row["eligible"]:
        return PersonalFactAnswer(unknown, "UNKNOWN")
    if row["disputed"]:
        return PersonalFactAnswer(
            "My saved information on that is disputed, so I can't reliably answer yet.",
            "DISPUTED",
        )
    value = row["value_text"]
    if not source_supports_value(topic,value,row["original_source_text"],row["source_quote"]):
        return PersonalFactAnswer(unknown, "UNSUPPORTED_SOURCE")
    # A saved user report is not proof of the current external state.
    return PersonalFactAnswer(
        f"Your last saved report says you {_VERBS[topic]}: {value}.",
        "KNOWN",row["memory_id"],row["version"],row["source_turn_id"],
    )


def resolve_saved_fact(cur, *, user_id: UUID, instance_id: UUID, topic: str,
                       before_turn_id: UUID) -> PersonalFactAnswer:
    """Called only in the selected, latest-turn fenced response transaction."""
    from .memory import read_personal_fact_slot
    row=read_personal_fact_slot(
        cur,user_id=user_id,instance_id=instance_id,topic=topic,
        before_turn_id=before_turn_id,
    )
    return render_saved_fact(topic,row)


def respond_with_personal_fact_recall(connection, actor, turn_id: UUID):
    """Direct read-only route; use normal durable response and replay contracts."""
    from .conversation import (
        commit_text_response,lookup_response,prepare_text_context,respond_to_text,
    )
    from .conversation_contracts import ModelReply
    from .operation_contracts import require_idle
    require_idle(connection)
    if lookup_response(connection,actor,turn_id) is not None:
        class NoRegeneration:
            def generate(self,messages):
                raise AssertionError("persisted personal-fact reply must not regenerate")
        return respond_to_text(connection,actor,turn_id,NoRegeneration())
    context=prepare_text_context(connection,actor,turn_id)
    if personal_fact_topic(context.messages[-1].content) is None:
        raise ValueError("personal-fact recall requires a supported direct question")
    # The placeholder is never delivered: the commit must re-derive the
    # topic from authenticated persisted input and read the current slot.
    return commit_text_response(
        connection,actor,context,
        ModelReply(model_id=ROUTE_ID,text="Personal-fact recall."),
        personal_fact_recall=True,
    )
