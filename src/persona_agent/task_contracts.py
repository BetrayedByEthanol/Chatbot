"""P3-02 task and priority contracts; no task operation dispatch authority.

The Goal domain owns intended outcomes; tasks are concrete authorized work,
not strategies or procedures. Completion claims require authenticated evidence.
"""
import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .goal_contracts import GoalUserCommand

Executor = Literal["USER", "AGENT"]
EvidencePolicy = Literal["USER_CONFIRMATION", "OPERATION_RESULT"]
TaskStatus = Literal["OPEN", "USER_CONFIRMED", "CANCELLED"]
TaskActionability = Literal[
    "READY", "WAITING_DEPENDENCIES", "GOAL_INACTIVE", "SUSPENDED",
    "EXCLUSIVE_OTHER", "NEEDS_CAPABILITY", "DEADLINE_EXPIRED",
    "COMPLETE_USER_CONFIRMED", "CANCELLED",
]
PriorityRule = Literal[
    "PREFER_OVER", "CLEAR_PREFERENCE", "RUN_AFTER", "CLEAR_RUN_AFTER",
    "SUSPEND", "RESUME", "SET_DEADLINE", "CLEAR_DEADLINE",
    "ONLY", "CLEAR_ONLY",
]


class TaskUserCommand(GoalUserCommand):
    """Explicit authenticated user source + optional action-bound approval."""


class CreateTaskCommand(TaskUserCommand):
    task_id: UUID
    goal_id: UUID
    expected_goal_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    action_description: str = Field(min_length=1, max_length=800)
    completion_criterion: str = Field(min_length=1, max_length=600)
    executor: Executor
    capability: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_task(self):
        if self.executor == "USER" and self.capability is not None:
            raise ValueError("user action cannot claim an agent capability")
        if not all(x.strip() for x in (
            self.title, self.action_description, self.completion_criterion
        )):
            raise ValueError("task outcome and completion criterion must not be blank")
        return self


class AddTaskDependencyCommand(TaskUserCommand):
    task_id: UUID
    depends_on_id: UUID
    expected_task_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_dependency(self):
        if self.task_id == self.depends_on_id:
            raise ValueError("task cannot depend on itself")
        return self


class ConfirmUserTaskCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    report_id: UUID


class CancelTaskCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)


class SetTaskPriorityCommand(TaskUserCommand):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    rule: PriorityRule
    related_task_id: UUID | None = None
    deadline: datetime | None = None

    @model_validator(mode="after")
    def validate_priority(self):
        relative = self.rule in ("PREFER_OVER", "RUN_AFTER")
        if relative != (self.related_task_id is not None):
            raise ValueError("relative rule requires exactly one related task")
        if self.related_task_id == self.task_id:
            raise ValueError("priority cannot refer to itself")
        if (self.rule == "SET_DEADLINE") != (self.deadline is not None):
            raise ValueError("deadline required only for SET_DEADLINE")
        if self.deadline is not None and (
            self.deadline.tzinfo is None or self.deadline.utcoffset() is None
        ):
            raise ValueError("deadline must have timezone")
        return self


class TaskCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: UUID
    task_version: int = Field(ge=1)
    event_id: UUID
    action: Literal[
        "TASK_CREATED", "DEPENDENCY_ADDED", "USER_COMPLETION_RECORDED",
        "TASK_CANCELLED", "PRIORITY_CHANGED",
    ]
    replayed: bool = False


class TaskView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: UUID
    goal_id: UUID
    title: str
    action_description: str
    completion_criterion: str
    executor: Executor
    evidence_policy: EvidencePolicy
    capability: str | None
    status: TaskStatus
    version: int = Field(ge=1)
    actionability: TaskActionability
    unmet_dependencies: tuple[UUID, ...] = ()
    suspended: bool
    exclusive_only: bool
    preferred_over_task_id: UUID | None
    run_after_task_id: UUID | None
    deadline: datetime | None
    # No task-owned operation can be admitted through P3-02.
    dispatch_authorized: Literal[False] = False


def task_approval_phrase(command: TaskUserCommand) -> str:
    """Bind user confirmation to the full proposed task/priority state change.

    This challenge is not an LLM prediction or a general approval token.
    Conversation/UI must present the action before collecting this phrase.
    """
    if not isinstance(command, (
        CreateTaskCommand, AddTaskDependencyCommand, ConfirmUserTaskCommand,
        CancelTaskCommand, SetTaskPriorityCommand,
    )):
        raise ValueError("unsupported task action")
    payload = command.model_dump(
        mode="json",
        exclude={"approval_turn_id", "idempotency_key", "expected_generation"},
    )
    encoded = json.dumps(
        {"command_type":type(command).__name__,"payload":payload},
        sort_keys=True, separators=(",",":"),
    ).encode("utf-8")
    return "APPROVE TASK CHANGE " + hashlib.sha256(encoded).hexdigest()[:24].upper()
