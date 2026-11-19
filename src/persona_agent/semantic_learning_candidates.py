"""MVP-02C.14D: offline learning candidates, not an autonomous training pipeline.

A candidate is scoped evaluation evidence. Model output never supplies consent,
independent adjudication, source authority, or permission to export data.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .semantic_dimension_contracts import SemanticDimensionsV2, TAXONOMY_VERSION

CandidateTrigger = Literal[
    "USER_CLARIFICATION", "INTERPRETATION_ERROR", "MODEL_DISAGREEMENT",
    "UNRESOLVED_AMBIGUITY", "MULTI_CLAIM", "QUALIFIER_RISK",
]
EvidenceKind = Literal[
    "USER_CLARIFICATION", "CONVERSATION_CORRECTION",
    "MODEL_PROPOSAL", "MODEL_DISAGREEMENT", "RULE_DIAGNOSTIC",
]
ReviewStatus = Literal["UNREVIEWED", "INDEPENDENTLY_REVIEWED", "REJECTED"]
PrivacyClass = Literal["NORMAL", "SENSITIVE", "UNKNOWN"]


class LearningEvidenceRef(BaseModel):
    """Source pointer only; source existence/ownership is verified externally."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_turn_id: UUID
    kind: EvidenceKind


class SemanticLearningCandidate(BaseModel):
    """A potential annotation, NOT training permission or verified truth.

    Metadata IDs and review status must come from the trusted host / reviewer,
    never directly from classifier or generative model outputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    user_id: UUID
    instance_id: UUID
    interpretation: SemanticDimensionsV2
    trigger: CandidateTrigger
    evidence_refs: tuple[LearningEvidenceRef, ...] = Field(min_length=1, max_length=16)
    review_status: ReviewStatus = "UNREVIEWED"
    review_ref: UUID | None = None
    privacy_class: PrivacyClass = "UNKNOWN"

    @model_validator(mode="after")
    def reviewed_labels_need_review_reference(self):
        if (self.review_status == "INDEPENDENTLY_REVIEWED") != (
            self.review_ref is not None
        ):
            raise ValueError("independent review status requires review reference")
        return self


class TrainingUseContext(BaseModel):
    """Trusted owner-policy snapshot. Must NOT be populated from model output.

    An offline export must also recheck current consent, retention, source
    ownership, source revisions and reviewer identity against PostgreSQL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    instance_id: UUID
    explicit_training_consent: bool = False
    retention_permits_training: bool = False
    authorized_turn_ids: frozenset[UUID] = frozenset()
    independently_verified_review_refs: frozenset[UUID] = frozenset()


class TrainingCandidateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: Literal["REJECT", "REVIEW_REQUIRED", "ELIGIBLE_FOR_OFFLINE_EXPORT"]
    reason: str = Field(min_length=1, max_length=80)


def assess_training_candidate(
    candidate: SemanticLearningCandidate, context: TrainingUseContext,
) -> TrainingCandidateDecision:
    """Pure eligibility screening; never exports data, trains or deploys.

    Evidence type MODEL_DISAGREEMENT or agreement is not a verified label.
    An eligible decision is advisory and still requires fresh owner checks.
    """
    if not isinstance(candidate, SemanticLearningCandidate) or not isinstance(
        context, TrainingUseContext
    ):
        raise TypeError("validated candidate and trusted use context required")

    def result(disposition: str, reason: str) -> TrainingCandidateDecision:
        return TrainingCandidateDecision(disposition=disposition, reason=reason)

    if (candidate.user_id != context.user_id
            or candidate.instance_id != context.instance_id):
        return result("REJECT", "CROSS_INSTANCE")
    if not context.explicit_training_consent:
        return result("REJECT", "NO_EXPLICIT_TRAINING_CONSENT")
    if not context.retention_permits_training:
        return result("REJECT", "RETENTION_DISALLOWS_TRAINING")
    if candidate.privacy_class != "NORMAL":
        return result("REJECT", "PRIVACY_REVIEW_REQUIRED")
    if candidate.interpretation.taxonomy_version != TAXONOMY_VERSION:
        return result("REJECT", "STALE_TAXONOMY")
    used_turns = {candidate.interpretation.source_turn_id}
    used_turns.update(ref.source_turn_id for ref in candidate.evidence_refs)
    if not used_turns.issubset(context.authorized_turn_ids):
        return result("REJECT", "SOURCE_NOT_AUTHORIZED")
    if candidate.review_status == "REJECTED":
        return result("REJECT", "ANNOTATION_REJECTED")
    if candidate.review_status != "INDEPENDENTLY_REVIEWED":
        return result("REVIEW_REQUIRED", "NO_INDEPENDENT_LABEL_REVIEW")
    if candidate.review_ref not in context.independently_verified_review_refs:
        return result("REVIEW_REQUIRED", "REVIEW_NOT_VERIFIED")
    return result("ELIGIBLE_FOR_OFFLINE_EXPORT", "ADVISORY_ONLY")
