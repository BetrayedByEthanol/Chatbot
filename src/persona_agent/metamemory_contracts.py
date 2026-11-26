"""Metadata about observed retrieval, never epistemic authority."""
from datetime import datetime
from typing import Literal
from uuid import UUID
import hashlib
import json

from pydantic import BaseModel,ConfigDict,Field,model_validator

RecallRoute=Literal["FACT","MEANING","ENTITY","ENTITY_HISTORY","PATTERN","KNOWLEDGE"]
RecallOutcome=Literal["AVAILABLE","UNKNOWN","AMBIGUOUS","CONTRADICTORY",
                      "UNVERIFIABLE","STALE","NO_EVIDENCE","BUDGET"]
FeedbackOutcome=Literal["RELEVANT","IRRELEVANT","INCORRECT","MISSED"]
_OUTCOMES={
    "KNOWN":"AVAILABLE","UNKNOWN":"UNKNOWN","AMBIGUOUS":"AMBIGUOUS",
    "DISPUTED":"CONTRADICTORY","UNSUPPORTED_SOURCE":"UNVERIFIABLE",
    "STALE":"STALE","NO_EVIDENCE":"NO_EVIDENCE","BUDGET":"BUDGET",
}


def recall_key(route: RecallRoute, topic: str) -> str:
    return hashlib.sha256(json.dumps(
        [route," ".join(topic.casefold().split())],ensure_ascii=False,
        separators=(",",":"),
    ).encode("utf-8")).hexdigest()


def candidate_key(candidates) -> str:
    return hashlib.sha256(json.dumps(sorted(candidates),separators=(",",":"))
                          .encode("utf-8")).hexdigest()


def recall_outcome(status: str) -> RecallOutcome:
    return _OUTCOMES[status]


class RetrievalFeedbackCommand(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    instance_id: UUID
    expected_generation: int=Field(ge=1)
    idempotency_key: str=Field(min_length=1,max_length=128)
    conversation_id: UUID
    feedback_turn_id: UUID
    response_id: UUID
    outcome: FeedbackOutcome
    passage_ordinal: int | None=Field(default=None,ge=1,le=8)

    @model_validator(mode="after")
    def target(self):
        if self.passage_ordinal is not None and self.outcome not in ("RELEVANT","IRRELEVANT"):
            raise ValueError("passage feedback supports relevance only")
        return self


def feedback_phrase(command: RetrievalFeedbackCommand) -> str:
    if command.passage_ordinal is not None:
        return f"Saved passage {command.passage_ordinal} was {command.outcome.lower()}."
    return {
        "RELEVANT":"That recall was relevant.",
        "IRRELEVANT":"That recall was irrelevant.",
        "INCORRECT":"That recall was incorrect.",
        "MISSED":"You missed relevant saved information.",
    }[command.outcome]


class RetrievalFeedbackResult(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    feedback_id: UUID
    event_id: UUID
    replayed: bool=False


class RecallObservation(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    response_id: UUID
    conversation_id: UUID
    turn_id: UUID
    route: RecallRoute
    query_key: str
    outcome: RecallOutcome
    evidence_refs: tuple[dict,...]
    clarification_suppressed: bool
    observed_at: datetime
    delivered_at: datetime | None
    feedback: tuple[FeedbackOutcome,...]=()
    # Original outcome is audit history; this flag is current eligibility.
    source_current: bool = True
