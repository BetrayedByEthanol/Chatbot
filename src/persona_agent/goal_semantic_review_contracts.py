"""MVP-03A.3: bounded semantic Goal review and owner-only adoption."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Alignment = Literal["ALIGNED","UNCERTAIN","DRIFT_DETECTED","BLOCKED"]
Decision = Literal["ACCEPTED","DEFERRED","REJECTED","SUPERSEDED"]


class ReviewSubgoalCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id: UUID
    expected_generation: int=Field(ge=1)
    proposal_id: UUID
    review_id: UUID
    idempotency_key: str=Field(min_length=1,max_length=128)


class SemanticSubgoalFinding(BaseModel):
    """An untrusted, bounded LLM assessment. No permission field grants rights."""
    model_config=ConfigDict(frozen=True,extra="forbid")
    alignment: Alignment
    parent_evidence_quote: str=Field(min_length=3,max_length=300)
    child_evidence_quote: str=Field(min_length=3,max_length=300)
    contribution_reason: str=Field(min_length=10,max_length=500)
    within_parent_scope: StrictBool
    measurable: StrictBool
    supports_not_replaces_parent: StrictBool
    changes_parent_objective: StrictBool
    requires_new_permissions: StrictBool
    creates_new_user_commitment: StrictBool

    @model_validator(mode="after")
    def require_nonblank(self):
        if not all(x.strip() for x in (
            self.parent_evidence_quote,self.child_evidence_quote,
            self.contribution_reason,
        )):
            raise ValueError("semantic review requires nonblank evidence")
        return self


class SemanticSubgoalReviewResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    proposal_id: UUID
    review_id: UUID
    alignment: Alignment
    decision: Decision
    event_id: UUID
    accepted_goal_event_id: UUID | None = None
    replayed: bool=False
