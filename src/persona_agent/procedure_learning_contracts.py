"""P3-05: evidence-linked method assessment and explicitly approved private tuning.

An observation is never a verified Goal or Task outcome. Only the Procedure
Manager may admit run assessments or method proposals; no self-authorized edits.
"""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .procedure_contracts import ParameterValue, ProcedureUserCommand

ReportedOutcome = Literal[
    "UNKNOWN", "INCOMPLETE", "USER_REPORTED_SUCCESS", "USER_REPORTED_FAILURE",
]
FailureCause = Literal[
    "UNDETERMINED", "INCORRECT_INPUT", "ENVIRONMENT",
    "INCOMPLETE_USER_ACTION", "METHOD_MISMATCH", "INVALID_SUCCESS_CRITERIA",
]
AdaptationDecision = Literal["APPLY", "REJECT"]
CriterionState = Literal["UNKNOWN", "USER_REPORTED_MET", "USER_REPORTED_UNMET"]


class ProcedureCriterionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    position: int = Field(ge=1, le=12)
    state: CriterionState


class EvaluateProcedureRunCommand(ProcedureUserCommand):
    evaluation_id: UUID
    run_id: UUID
    expected_run_version: int = Field(ge=1)
    expected_previous_evaluation_id: UUID | None = None
    outcome: ReportedOutcome
    failure_cause: FailureCause | None = None
    rationale: str = Field(min_length=10, max_length=800)
    # Direct immutable run-step event references, not a model's unsourced claim.
    supporting_step_event_ids: tuple[UUID, ...] = Field(default=(), max_length=20)
    criteria: tuple[ProcedureCriterionReport, ...] = Field(default=(), max_length=12)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_outcome(self):
        if self.outcome == "USER_REPORTED_FAILURE" and self.failure_cause is None:
            raise ValueError("failure reports must classify cause or UNDETERMINED")
        if self.outcome != "USER_REPORTED_FAILURE" and self.failure_cause is not None:
            raise ValueError("failure cause only applies to reported failures")
        if len(set(self.supporting_step_event_ids)) != len(self.supporting_step_event_ids):
            raise ValueError("duplicate step evidence")
        if len({item.position for item in self.criteria}) != len(self.criteria):
            raise ValueError("duplicate success criterion position")
        if not self.rationale.strip():
            raise ValueError("assessment rationale cannot be blank")
        return self


class ProposeProcedureAdaptationCommand(ProcedureUserCommand):
    proposal_id: UUID
    evaluation_id: UUID
    variant_id: UUID
    expected_revision: int = Field(ge=1)
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    step_text_overrides: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(min_length=10, max_length=800)

    @model_validator(mode="after")
    def validate_reason(self):
        if not self.reason.strip():
            raise ValueError("adaptation rationale cannot be blank")
        return self


class DecideProcedureAdaptationCommand(ProcedureUserCommand):
    proposal_id: UUID
    decision: AdaptationDecision
    expected_revision: int = Field(ge=1)


class ProcedureLearningResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_id: UUID
    event_id: UUID
    action: Literal["RUN_EVALUATED", "ADAPTATION_PROPOSED",
                    "ADAPTATION_APPLIED", "ADAPTATION_REJECTED"]
    revision: int
    replayed: bool = False


class ProcedureEvaluationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evaluation_id: UUID
    run_id: UUID
    evaluation_revision: int
    run_version: int
    supersedes_evaluation_id: UUID | None
    outcome: ReportedOutcome
    failure_cause: FailureCause | None
    rationale: str
    confidence: float
    supporting_step_event_ids: tuple[UUID, ...]
    criteria: tuple[ProcedureCriterionReport, ...]
    # Outcome is user testimony or a tentative observation, not verified.
    independently_verified: Literal[False] = False
    task_or_goal_completed: Literal[False] = False


class ProcedureAdaptationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    proposal_id: UUID
    evaluation_id: UUID
    run_id: UUID
    variant_id: UUID
    expected_revision: int
    template_version: int
    policy: Literal["PARAMETERIZED", "ADAPTIVE"]
    parameters: dict[str, ParameterValue]
    step_text_overrides: dict[str, str]
    reason: str
    status: Literal["PROPOSED", "APPLIED", "REJECTED"]
    applied_revision: int | None
