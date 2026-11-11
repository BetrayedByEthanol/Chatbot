"""P2-02 summary and episodic memory contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


SUMMARY_OPERATION = "memory.summarize_range.v1"
SUMMARY_RESOURCE = "cognition.llm"
SUMMARY_CAPABILITY = "memory.summarization"


class EpisodeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_turn_id: UUID
    evidence_quote: str = Field(min_length=1, max_length=1000)
    topic: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1200)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_quote", "topic", "description")
    @classmethod
    def strip_content(cls, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("episode content cannot be blank")
        return text


class SummaryDraft(BaseModel):
    """The model proposes interpretations; it cannot assert verified facts."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4000)
    episodes: tuple[EpisodeDraft, ...] = Field(default=(), max_length=3)

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("empty summary")
        return text


class SummaryScheduled(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    conversation_id: UUID
    start_sequence: int
    end_sequence: int
    replayed: bool = False


class SummaryCommitted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_id: UUID
    conversation_id: UUID
    start_sequence: int
    end_sequence: int
    episode_count: int
    replayed: bool = False


class EpisodeHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: UUID
    conversation_id: UUID
    source_turn_id: UUID
    source_sequence: int
    occurred_at: datetime
    topic: str
    description: str
    epistemic_status: Literal["MODEL_INTERPRETED"]
    confidence: float


class SummaryNotReady(Exception):
    """There are no safe, closed turns to summarize under the given policy."""


class StaleSummary(Exception):
    """A source-range, watermark, operation or persona fence is no longer valid."""


class InvalidSummary(Exception):
    """Model returned a malformed or ungrounded proposal."""


@dataclass(frozen=True)
class SummaryWork:
    operation_id: UUID
    user_id: UUID
    instance_id: UUID
    conversation_id: UUID
    start_sequence: int
    end_sequence: int
    source_digest: str
    turns: tuple[dict, ...]
    completed_summary_id: UUID | None = None
