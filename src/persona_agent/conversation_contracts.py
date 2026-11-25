"""Phase 1 text conversation contracts; persistence remains PostgreSQL-owned."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .temporal_context import TemporalContext


class TextInputCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("input must contain non-whitespace text")
        return value


class TextInputResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    turn_id: UUID
    event_id: UUID
    sequence: int
    replayed: bool = False


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class ModelIntentCandidate(BaseModel):
    """Tentative model interpretation; source evidence is independently checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["OBJECTIVE", "REQUEST", "INSTRUCTION", "CORRECTION",
                  "OBSERVATION", "HYPOTHETICAL"]
    evidence_quote: str = Field(min_length=3, max_length=200)
    confidence: float = Field(ge=0, le=1)
    reference: Literal["NONE", "PRIOR_OBJECTIVE"] = "NONE"


@dataclass(frozen=True)
class ModelReply:
    model_id: str
    text: str
    intent_candidate: ModelIntentCandidate | None = None


@dataclass(frozen=True)
class ProcedureContextFence:
    """Source versions used to construct the displayed procedure context."""
    run_id: UUID
    run_version: int
    prior_conversation_id: UUID | None = None
    prior_conversation_revision: int | None = None


@dataclass(frozen=True)
class PreferenceReadFence:
    """Relevant Relationship projection versions used by a direct recall.

    Missing keys are represented by their absence from revisions but their
    inclusion in keys; removal is represented by a revision with status
    REMOVED. No Relationship write authority is granted to Conversation.
    """
    keys: tuple[str, ...]
    revisions: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class HistoricalReadFence:
    """Conservative scoped revision and next eligibility expiry."""
    revision: int
    next_expiry: datetime | None


@dataclass(frozen=True)
class ResponseContext:
    conversation_id: UUID
    turn_id: UUID
    instance_id: UUID
    selection_generation: int
    conversation_revision: int
    messages: tuple[ModelMessage, ...]
    procedure_fence: ProcedureContextFence | None = None
    preference_fence: PreferenceReadFence | None = None
    temporal_context: TemporalContext | None = None
    historical_fence: HistoricalReadFence | None = None


class TextModelGateway(Protocol):
    def generate(self, messages: Sequence[ModelMessage]) -> ModelReply:
        """Make a model request; never use a database transaction here."""


class TextResponseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    response_id: UUID
    turn_id: UUID
    text: str
    model_id: str
    delivery_status: Literal["GENERATED", "DELIVERED"]
    replayed: bool = False


class StaleConversation(Exception):
    """The reply context or persona authority was superseded."""


class StaleHistoricalContext(StaleConversation):
    """Optional historical evidence changed; the same input can be retried."""


class ModelUnavailable(Exception):
    """A model failed; its already-committed user input is retained."""
