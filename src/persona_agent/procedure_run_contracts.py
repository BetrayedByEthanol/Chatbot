"""P3-04 durable run, step-evidence and conversation-link contracts.

ProcedureRun state represents attempts and user testimony, not Task or Goal
success and never an independent verification or authorization to use tools.
"""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .procedure_contracts import ProcedureUserCommand, ProcedureStep, ParameterValue

RunStatus = Literal["ACTIVE", "PAUSED", "NEEDS_REVIEW", "USER_CONFIRMED", "CANCELLED"]
StepStatus = Literal[
    "NOT_STARTED", "INSTRUCTED", "ATTEMPTED", "USER_REPORTED_DONE",
    "USER_CONFIRMED", "FAILED", "BLOCKED", "NEEDS_REVIEW",
]
UserStepOutcome = Literal[
    "ATTEMPTED", "USER_REPORTED_DONE", "USER_CONFIRMED", "FAILED", "BLOCKED",
]


class StartProcedureRunCommand(ProcedureUserCommand):
    run_id: UUID
    template_id: UUID
    template_version: int = Field(ge=1)
    variant_id: UUID | None = None
    variant_revision: int | None = Field(default=None, ge=1)
    task_id: UUID | None = None
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def version_pins(self):
        if (self.variant_id is None) != (self.variant_revision is None):
            raise ValueError("private procedure runs require an exact variant revision")
        if self.variant_id is not None and self.parameters:
            raise ValueError("private run parameters come from the pinned variant revision")
        return self


class ProcedureRunChangeCommand(ProcedureUserCommand):
    run_id: UUID
    expected_run_version: int = Field(ge=1)


class RecordProcedureStepCommand(ProcedureRunChangeCommand):
    step_id: str = Field(min_length=1, max_length=40)
    expected_step_version: int = Field(ge=1)
    outcome: UserStepOutcome
    detail: str | None = Field(default=None, min_length=10, max_length=800)

    @model_validator(mode="after")
    def detail_for_obstacle(self):
        if self.outcome in ("FAILED", "BLOCKED") and (
            self.detail is None or not self.detail.strip()
        ):
            raise ValueError("failed/blocked steps require an explanation")
        return self


class AcknowledgeProcedureInstructionCommand(ProcedureRunChangeCommand):
    step_id: str = Field(min_length=1, max_length=40)
    expected_step_version: int = Field(ge=1)
    response_id: UUID


class CorrectProcedureStepCommand(ProcedureRunChangeCommand):
    step_id: str = Field(min_length=1, max_length=40)
    expected_step_version: int = Field(ge=1)
    superseded_step_event_id: UUID
    reason: str = Field(min_length=10, max_length=800)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if len(self.reason.strip()) < 10:
            raise ValueError("step correction needs a substantive reason")
        return self


class LinkProcedureThreadCommand(ProcedureRunChangeCommand):
    thread_id: UUID


class SetProcedureRunStatusCommand(ProcedureRunChangeCommand):
    action: Literal["PAUSE", "RESUME", "CANCEL", "FINISH"]


class ProcedureRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    version: int
    event_id: UUID
    action: Literal[
        "RUN_STARTED", "STEP_RECORDED", "INSTRUCTION_DELIVERED",
        "STEP_CORRECTED", "THREAD_LINKED", "RUN_PAUSED",
        "RUN_RESUMED", "RUN_CANCELLED", "RUN_USER_CONFIRMED",
    ]
    step_id: str | None = None
    step_version: int | None = None
    step_event_id: UUID | None = None
    replayed: bool = False


class RenderedProcedureStep(ProcedureStep):
    """A persisted step after parameter interpolation.

    Published templates use the tighter ProcedureStep limits (800/400);
    validate_variant and migration 0018 admit rendered text up to 1200/800.
    Keep all inherited step identity/executor/capability validation.
    """
    instruction: str = Field(min_length=1, max_length=1200)
    success_test: str = Field(min_length=1, max_length=800)


class ProcedureRunStepView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str
    position: int
    step: RenderedProcedureStep
    status: StepStatus
    version: int
    last_step_event_id: UUID | None
    # Neither an instruction nor a user report is verified external execution.
    independently_verified: Literal[False] = False


class ProcedureRunThreadLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    thread_id: UUID


class ProcedureRunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    template_id: UUID
    template_version: int
    variant_id: UUID | None
    variant_revision: int | None
    task_id: UUID | None
    status: RunStatus
    version: int
    steps: tuple[ProcedureRunStepView, ...]
    thread_links: tuple[ProcedureRunThreadLink, ...]
    next_step_id: str | None
    # A user-confirmed run never mutates task completion on its own.
    task_completion_changed: Literal[False] = False
    operation_dispatch_authorized: Literal[False] = False
