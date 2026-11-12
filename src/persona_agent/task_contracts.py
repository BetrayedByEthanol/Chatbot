"""Phase 3 P3-02: bounded task DAG and evidence/actionability contracts.

A task describes work and evidence requirements, not an executable operation.
No task can be verified by advice, prediction, or its own user report.
"""
import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

Executor = Literal["USER", "AGENT"]
TaskStatus = Literal["PLANNED", "USER_REPORTED_DONE", "USER_CONFIRMED", "CANCELLED"]
EvidencePolicy = Literal["USER_CONFIRMATION", "OPERATION_RESULT"]
PriorityRule = Literal[
    "PREFER_OVER", "CLEAR_PREFERENCE", "SUSPEND", "RESUME",
    "SET_DEADLINE", "CLEAR_DEADLINE", "RUN_AFTER", "CLEAR_RUN_AFTER",
    "ONLY", "CLEAR_ONLY",
]

class TaskUserCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    conversation_id: UUID
    evidence_turn_id: UUID
    evidence_quote: str = Field(min_length=1, max_length=300)
    approval_turn_id: UUID | None = None

    @field_validator('evidence_quote')
    @classmethod
    def nonempty_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('source quote must not be blank')
        return value

class CreateTaskCommand(TaskUserCommand):
    task_id: UUID
    goal_id: UUID
    expected_goal_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    action_description: str = Field(min_length=1, max_length=800)
    completion_criterion: str = Field(min_length=1, max_length=800)
    evidence_requirement: str = Field(min_length=1, max_length=800)
    executor: Executor
    capability: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_task(self):
        if not all(x.strip() for x in (self.title, self.action_description, self.completion_criterion, self.evidence_requirement)):
            raise ValueError("task descriptions must not be blank")
        if self.executor == "AGENT" and not self.capability:
            raise ValueError("agent task needs a declared capability")
        if self.executor == "USER" and self.capability is not None:
            raise ValueError("user task cannot declare an agent capability")
        return self

class AddTaskDependencyCommand(TaskUserCommand):
    task_id: UUID
    prerequisite_task_id: UUID
    expected_goal_version: int = Field(ge=1)

    @model_validator(mode="after")
    def no_self_edge(self):
        if self.task_id == self.prerequisite_task_id:
            raise ValueError("task cannot depend on itself")
        return self

class CancelTaskCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)


class ReportTaskOutcomeCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    report: Literal["DONE", "OBSTACLE"]

class ConfirmUserTaskCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    report_id: UUID


class SetTaskPriorityCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    rule: PriorityRule
    other_task_id: UUID | None = None
    deadline: datetime | None = None

    @model_validator(mode="after")
    def rule_args(self):
        if self.rule in ("PREFER_OVER", "RUN_AFTER"):
            if self.other_task_id is None or self.other_task_id == self.task_id or self.deadline is not None:
                raise ValueError("relative priority needs distinct other task only")
        elif self.rule == "SET_DEADLINE":
            if self.deadline is None or self.other_task_id is not None or self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
                raise ValueError("deadline must be timezone-aware and standalone")
        elif self.other_task_id is not None or self.deadline is not None:
            raise ValueError("standalone priority changes cannot carry deadline or other task")
        return self

class TaskCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: UUID
    version: int
    event_id: UUID
    action: Literal["TASK_CREATED", "DEPENDENCY_ADDED", "OUTCOME_REPORTED", "USER_COMPLETION_CONFIRMED", "PRIORITY_SET", "TASK_CANCELLED"]
    report_id: UUID | None = None
    replayed: bool = False

class TaskView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: UUID
    goal_id: UUID
    title: str
    action_description: str
    executor: Executor
    evidence_policy: EvidencePolicy
    completion_criterion: str
    evidence_requirement: str
    capability: str | None
    status: TaskStatus
    version: int
    actionable: bool
    blockers: tuple[str, ...]
    prerequisite_ids: tuple[UUID, ...]
    preferred_over: tuple[UUID, ...]
    run_after: tuple[UUID, ...]
    deadline: datetime | None
    exclusive_only: bool = False
    unmet_dependencies: tuple[UUID, ...] = ()
    # Readiness does not authorize tool execution; P0 still rejects task_id.
    dispatch_authorized: Literal[False] = False

def task_approval_phrase(command: TaskUserCommand) -> str:
    """A separate user turn confirms this exact action-specific payload."""
    payload = command.model_dump(mode="json", exclude={
        "approval_turn_id", "idempotency_key", "expected_generation"
    })
    encoded = json.dumps({"action": type(command).__name__, "payload": payload},
                         sort_keys=True, separators=(",", ":")).encode()
    return "APPROVE TASK CHANGE " + hashlib.sha256(encoded).hexdigest()[:24].upper()
