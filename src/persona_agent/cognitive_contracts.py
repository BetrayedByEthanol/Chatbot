"""P2-06 bounded, evidence-linked asynchronous cognitive contracts."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

COGNITIVE_OPERATION = "cognition.reflect_range.v1"
COGNITIVE_CAPABILITY = "cognition.reflection"
COGNITIVE_RESOURCE = "cognition.llm"
COGNITIVE_CONSUMER = "cognition.auto_reflection.v1"

ReflectionKind = Literal[
    "OBSERVATION","HYPOTHESIS","CONCLUSION","QUESTION","SELF_EVALUATION",
    "INTENTION",
]
Subject = Literal["USER","CONVERSATION","PERSONA"]


class CognitiveProposal(BaseModel):
    """A concise interpretable *claim*, not hidden model reasoning."""

    model_config=ConfigDict(extra="forbid",frozen=True)
    kind: ReflectionKind
    subject: Subject
    content: str = Field(min_length=1,max_length=600)
    source_turn_id: UUID
    evidence_quote: str = Field(min_length=1,max_length=400)
    confidence: float = Field(ge=0,le=1)
    trigger_text: str | None = Field(default=None,min_length=1,max_length=200)
    expiry_days: int | None = Field(default=None,ge=1,le=30)

    @field_validator("content","evidence_quote","trigger_text")
    @classmethod
    def no_empty_text(cls,text):
        if text is not None and not text.strip():
            raise ValueError("cognitive text cannot be blank")
        return text

    @model_validator(mode="after")
    def intention_policy(self):
        if self.kind=="INTENTION":
            if self.trigger_text is None or self.expiry_days is None:
                raise ValueError("intention requires trigger and bounded expiry")
        elif self.trigger_text is not None or self.expiry_days is not None:
            raise ValueError("only intentions may specify trigger and expiry")
        return self


class CognitiveDraft(BaseModel):
    """No output is valid when a range contains nothing worth reflecting on."""

    model_config=ConfigDict(extra="forbid",frozen=True)
    proposals: tuple[CognitiveProposal,...] = Field(default=(),max_length=5)


class CognitiveScheduled(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    operation_id: UUID
    conversation_id: UUID
    start_sequence: int = Field(ge=1)
    end_sequence: int = Field(ge=1)
    replayed: bool=False


class CognitiveCommitted(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    operation_id: UUID
    conversation_id: UUID
    end_sequence: int
    reflection_count: int
    intention_count: int
    replayed: bool=False


@dataclass(frozen=True)
class CognitiveWork:
    operation_id: UUID
    user_id: UUID
    instance_id: UUID
    conversation_id: UUID
    expected_generation: int
    start_sequence: int
    end_sequence: int
    source_digest: str
    turns: tuple[dict,...]
    completed: bool=False


@dataclass(frozen=True)
class CognitiveScheduleTick:
    dispatched: int
    processed: int
    deferred: int
    admitted: int
    replayed: int
    retry_pending: int


class CognitiveNotReady(Exception):
    pass


class StaleCognition(Exception):
    pass


class InvalidCognitiveProposal(Exception):
    pass
