"""Phase 1 text conversation contracts; persistence remains PostgreSQL-owned."""

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


@dataclass(frozen=True)
class ModelReply:
    model_id: str
    text: str


@dataclass(frozen=True)
class ResponseContext:
    conversation_id: UUID
    turn_id: UUID
    instance_id: UUID
    selection_generation: int
    conversation_revision: int
    messages: tuple[ModelMessage, ...]


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


class ModelUnavailable(Exception):
    """A model failed; its already-committed user input is retained."""
