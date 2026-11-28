"""Phase 1 text conversation contracts; persistence remains PostgreSQL-owned."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, Sequence
from uuid import UUID
from .learning_control_contracts import PredictiveFence
from .prediction_contracts import ComparisonRead

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .temporal_context import TemporalContext
from .meta_episodic_contracts import PatternRead


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
class EntityHistoryRead:
    """Evidence selected for this model context, not claims made by the model."""
    status: Literal["KNOWN","UNKNOWN","AMBIGUOUS","NO_EVIDENCE","BUDGET"]
    entity_id: UUID | None=None
    entity_version: int | None=None
    source_turn_ids: tuple[UUID,...]=()


@dataclass(frozen=True)
class RawContextFence:
    """Exact raw source IDs actually supplied by a bounded foreground context."""
    source_turn_ids: tuple[UUID,...]
    reusable: bool=True


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
    entity_history: EntityHistoryRead | None = None
    pattern_history: PatternRead | None = None
    meaning_change_action: Literal["REPORT","BLOCKED_ROUTINE"] | None = None
    goal_dialogue_action: bool = False
    procedure_guidance_action: bool = False
    raw_context_fence: RawContextFence | None = None
    presentation_fence: HistoricalReadFence | None = None
    # Host-only optional contribution. No predictive producer in P2-F1.1.
    predictive_fence: PredictiveFence | None = None
    # Diagnostic host signal only: not part of model messages or delivery.
    prediction_comparison: ComparisonRead | None = None


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


class InvalidatedResponse(StaleConversation):
    """A committed response cannot be offered or replayed from stale evidence."""


class ModelUnavailable(Exception):
    """A model failed; its already-committed user input is retained."""
