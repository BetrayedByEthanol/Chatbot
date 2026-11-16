"""Conversation Orchestrator Stage A: source-grounded semantic claim contracts.

Classifier outputs are advisory. They do not authorize Memory, Goal, Dialogue
or other domain writes. Multiple claims may originate from one user turn.
This module deliberately does not prescribe a model, inference process or DB.
"""
from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

StatementType = Literal["FACT", "PREFERENCE", "EXPERIENCE", "INTENTION", "OPINION", "UNKNOWN"]
TemporalScope = Literal[
    "CURRENT", "HABITUAL", "PAST_EVENT", "PAST_HABITUAL", "FUTURE", "UNSPECIFIED",
]
Modality = Literal[
    "ASSERTED", "TENTATIVE", "CONDITIONAL", "HYPOTHETICAL", "UNKNOWN",
]
PragmaticFunction = Literal[
    "INFORMING", "REQUESTING", "CORRECTING", "ASKING", "UNKNOWN",
]
ClassifierRoute = Literal["DETERMINISTIC", "HUGGING_FACE", "LLM"]
AbstentionReason = Literal["NO_SUPPORTED_CLAIM", "AMBIGUOUS", "OUT_OF_SCOPE"]


class SemanticClaim(BaseModel):
    """One source span with partial advisory labels, never accepted knowledge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_quote: str = Field(min_length=3, max_length=1000)
    # Specialized classifiers may explicitly return UNKNOWN in dimensions
    # outside their competence; no label may be fabricated to fill the schema.
    statement_type: StatementType
    temporal_scope: TemporalScope
    modality: Modality
    pragmatic_function: PragmaticFunction
    # A classifier proposes a topic label; Conversation owns topic identity.
    topic_label: str | None = Field(default=None, min_length=2, max_length=80)
    confidence: float = Field(ge=0, le=1)


class SemanticAnnotationBatch(BaseModel):
    """Versioned source-linked advisory output from one classifier invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_turn_id: UUID
    classifier_id: str = Field(min_length=1, max_length=128)
    classifier_version: str = Field(min_length=1, max_length=128)
    route: ClassifierRoute
    claims: tuple[SemanticClaim, ...] = Field(default=(), max_length=4)
    abstention_reason: AbstentionReason | None = None

    @model_validator(mode="after")
    def validate_abstention(self):
        if bool(self.claims) == (self.abstention_reason is not None):
            raise ValueError("abstention reason required iff no claims")
        return self


class SemanticClassifier(Protocol):
    """Replaceable, side-effect-free classifier adapter.

    Its contract is independent of Hugging Face runtime, process placement,
    model revision and optional concurrent scheduling.
    """

    def classify(
        self, text: str, *, source_turn_id: UUID,
    ) -> SemanticAnnotationBatch:
        ...


def validate_semantic_evidence(
    batch: SemanticAnnotationBatch, *, source_turn_id: UUID, source_text: str,
) -> SemanticAnnotationBatch:
    """Reject hallucinated evidence and cross-turn outputs at the trust boundary.

    Lexical evidence grounding cannot verify inferred meaning or confidence:
    only evaluation and downstream domain admission can assess those.
    """
    if type(batch) is not SemanticAnnotationBatch:
        raise TypeError("classifier must return a validated annotation batch")
    if batch.source_turn_id != source_turn_id:
        raise ValueError("classifier source turn mismatched the authenticated turn")
    for claim in batch.claims:
        if claim.evidence_quote not in source_text:
            raise ValueError("semantic claim quote is not verbatim source evidence")
    return batch
