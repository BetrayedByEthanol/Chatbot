"""P3-04 authorized AGENT-step execution, limited to registered read-only tools.

Permission is an exact authenticated, separately approved user command.
A verified *operation* result is not independent proof of task/goal success.
"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .procedure_run_contracts import ProcedureRunChangeCommand


class AuthorizeAgentStepCommand(ProcedureRunChangeCommand):
    operation_id: UUID
    step_id: str = Field(min_length=1, max_length=40)
    expected_step_version: int = Field(ge=1)
    expected_task_version: int = Field(ge=1)
    max_words: int = Field(default=5, ge=1, le=20)



class AuthorizeBackgroundAgentStepCommand(AuthorizeAgentStepCommand):
    """Explicit user opt-in to ONE read-only step in ONE future Background epoch.

    Approvals use the standard exact procedure_approval_phrase; previous P3
    Foreground authorizations never imply Background consent.
    """
    expected_background_epoch: int = Field(ge=1)


class RenewBackgroundAgentStepCommand(AuthorizeBackgroundAgentStepCommand):
    """New explicit next-epoch user approval to restart a safely stopped step.

    A NEW operation and command receipt are required. Never revives the old
    P0 lease/epoch or silently inherits its permission/attempt budget.
    """
    resume_from_operation_id: UUID
    resume_checkpoint_ref: str = Field(min_length=1,max_length=1000)


class BackgroundAgentDispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    task_id: UUID
    background_epoch: int
    replayed: bool = False


class AgentStepAuthorizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    run_id: UUID
    task_id: UUID
    event_id: UUID
    action: Literal["AGENT_STEP_AUTHORIZED"] = "AGENT_STEP_AUTHORIZED"
    replayed: bool = False


class AgentStepReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    run_id: UUID
    resolution: Literal["VERIFIED", "FAILED", "BLOCKED", "OUTCOME_UNKNOWN", "INVALIDATED"]
    step_event_id: UUID | None = None
    replayed: bool = False
