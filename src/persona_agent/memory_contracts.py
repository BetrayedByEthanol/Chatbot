"""P2-01 memory-domain contracts: user-reported claims, not verified truth.

Memory type, persistence scope and attention are independent concepts. This
slice admits only explicit user-evidenced factual and semantic records.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


MemoryKind = Literal["FACT", "PREFERENCE", "PERSONAL_MEANING"]


class AdmitMemoryCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: UUID
    expected_generation: int = Field(ge=1)
    conversation_id: UUID
    evidence_turn_id: UUID
    evidence_quote: str = Field(min_length=1, max_length=2000)
    memory_kind: MemoryKind
    subject_key: str = Field(min_length=1, max_length=64)
    predicate_key: str = Field(min_length=1, max_length=64)
    value_text: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    privacy_class: Literal["NORMAL"] = "NORMAL"
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("subject_key", "predicate_key")
    @classmethod
    def key_format(cls, value: str) -> str:
        normalized = "_".join(value.casefold().split())
        if not normalized or not all(c.isalnum() or c in "_-." for c in normalized):
            raise ValueError("memory keys must be alphanumeric with _, - or .")
        return normalized

    @field_validator("evidence_quote", "value_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory content must not be blank")
        return normalized


class AmendMemoryCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: UUID
    expected_generation: int = Field(ge=1)
    memory_id: UUID
    expected_version: int = Field(ge=1)
    conversation_id: UUID
    evidence_turn_id: UUID
    evidence_quote: str = Field(min_length=1, max_length=2000)
    action: Literal["CORRECT", "RETRACT"]
    new_value: str | None = Field(default=None, max_length=2000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("evidence_quote")
    @classmethod
    def normalized_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence quote must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "CORRECT":
            if self.new_value is None or not self.new_value.strip() or self.confidence is None:
                raise ValueError("correction needs new_value and confidence")
        elif self.new_value is not None or self.confidence is not None:
            raise ValueError("retraction must not invent replacement content")
        return self


class MemoryWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: UUID
    version: int
    event_id: UUID | None = None
    unchanged: bool = False
    replayed: bool = False


class MemoryHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: UUID
    memory_kind: MemoryKind
    subject_key: str
    predicate_key: str
    value_text: str
    confidence: float
    epistemic_status: Literal["USER_REPORTED"]
    version: int
    source_conversation_id: UUID
    source_turn_id: UUID
    source_turn_at: datetime


class MemoryConflict(Exception):
    """A different claim occupies this slot; explicit correction is required."""
