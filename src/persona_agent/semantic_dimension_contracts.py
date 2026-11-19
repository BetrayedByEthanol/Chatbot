"""MVP-02C.14D offline semantic taxonomy: advisory, source-grounded dimensions.

This deliberately does not replace semantic_contracts.SemanticClaim, authorize
Memory admission/correction, or add a runtime inference stage.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .semantic_contracts import StatementType

TAXONOMY_VERSION = "mvp02c14d.semantic_dimensions.v1"
TemporalReference = Literal["PAST", "PRESENT", "FUTURE"]
Certainty = Literal["ASSERTED", "TENTATIVE", "UNKNOWN"]
Polarity = Literal["POSITIVE", "NEGATIVE", "UNKNOWN"]
QualifierKind = Literal["TIME", "PLACE", "CONDITION", "ATTRIBUTION", "OTHER"]
MessageAct = Literal["CORRECTION_CUE", "QUESTION", "REQUEST", "PRIOR_REFERENCE"]
RelationKind = Literal["STATE_CHANGE", "CONTRAST"]
AnnotationRoute = Literal["DETERMINISTIC", "HUGGING_FACE", "LLM", "AUTHOR_DRAFT"]


class SourceSpan(BaseModel):
    """Zero-based Python-string offsets, [start, end), against the entire turn."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("source span must have positive length")
        return self


class ContextQualifier(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: QualifierKind
    evidence: SourceSpan


class SemanticDimensionClaim(BaseModel):
    """Independent properties of one claimed proposition, not a Memory fact.

    Empty temporal_references means unresolved, not no temporal reference.
    None for a boolean dimension means unknown, not false.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    evidence: SourceSpan
    statement_type: StatementType
    temporal_references: tuple[TemporalReference, ...] = Field(default=(), max_length=3)
    habituality: bool | None = None
    certainty: Certainty = "UNKNOWN"
    conditionality: bool | None = None
    hypotheticality: bool | None = None
    polarity: Polarity = "UNKNOWN"
    subject_evidence: SourceSpan | None = None
    qualifiers: tuple[ContextQualifier, ...] = Field(default=(), max_length=6)

    @model_validator(mode="after")
    def unique_temporal_references(self):
        if len(set(self.temporal_references)) != len(self.temporal_references):
            raise ValueError("temporal references must be unique")
        return self


class MessageSignal(BaseModel):
    """Pragmatic *cue*, not verified correction intent or a Memory conflict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MessageAct
    evidence: SourceSpan


class ClaimRelation(BaseModel):
    """A reported change or contrast is NOT a conversational correction."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: RelationKind
    evidence: SourceSpan
    to_claim_index: int = Field(ge=0)
    from_claim_index: int | None = Field(default=None, ge=0)


class SemanticDimensionsV2(BaseModel):
    """Offline versioned interpretation; all provenance IDs are host supplied."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source_turn_id: UUID
    taxonomy_version: Literal["mvp02c14d.semantic_dimensions.v1"] = TAXONOMY_VERSION
    interpreter_id: str = Field(min_length=1, max_length=128)
    interpreter_version: str = Field(min_length=1, max_length=128)
    route: AnnotationRoute
    claims: tuple[SemanticDimensionClaim, ...] = Field(default=(), max_length=8)
    message_signals: tuple[MessageSignal, ...] = Field(default=(), max_length=8)
    relations: tuple[ClaimRelation, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def relation_indices(self):
        for relation in self.relations:
            if relation.to_claim_index >= len(self.claims):
                raise ValueError("relation target must reference an existing claim")
            if relation.from_claim_index is not None:
                if relation.from_claim_index >= len(self.claims):
                    raise ValueError("relation source must reference an existing claim")
                if relation.from_claim_index == relation.to_claim_index:
                    raise ValueError("relation cannot link a claim to itself")
        return self


def validate_semantic_dimensions(
    interpretation: SemanticDimensionsV2, *, source_turn_id: UUID, source_text: str,
) -> SemanticDimensionsV2:
    """Verify quotes/offsets against the COMPLETE original authenticated turn.

    This is only lexical grounding. It cannot establish that an inferred
    claim, omitted qualifier, correction target or privacy scope is correct.
    """
    if not isinstance(interpretation, SemanticDimensionsV2):
        raise TypeError("validated SemanticDimensionsV2 required")
    if interpretation.source_turn_id != source_turn_id:
        raise ValueError("source turn mismatch")
    if not isinstance(source_text, str):
        raise TypeError("source_text must be a string")

    spans = []
    for claim in interpretation.claims:
        spans.append(claim.evidence)
        if claim.subject_evidence is not None:
            spans.append(claim.subject_evidence)
        spans.extend(qual.evidence for qual in claim.qualifiers)
    spans.extend(signal.evidence for signal in interpretation.message_signals)
    spans.extend(relation.evidence for relation in interpretation.relations)

    for span in spans:
        # Python slices clamp oversized end offsets, so equality alone cannot
        # prove that the proposed source interval exists within the turn.
        if span.end > len(source_text) or source_text[span.start:span.end] != span.text:
            raise ValueError("unmatched source span or dropped source evidence")
    return interpretation
