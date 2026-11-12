"""P3-03 Procedure Manager contracts: reusable immutable methods, not execution.

The template is persona-global and publisher-approved. A personalized variant
is strictly User × Persona and remains pinned to an immutable template version.
"""
from __future__ import annotations

import hashlib
import json
import re
from string import Formatter
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .goal_contracts import GoalUserCommand

ProcedurePolicy = Literal["STABLE", "PARAMETERIZED", "ADAPTIVE"]
ParameterKind = Literal["TEXT", "INTEGER", "BOOLEAN", "CHOICE"]
Executor = Literal["USER", "AGENT"]
ParameterValue = str | int | bool

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ParameterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=40)
    kind: ParameterKind
    required: bool = True
    max_chars: int | None = Field(default=None, ge=1, le=256)
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def valid(self):
        if not _IDENT.fullmatch(self.name):
            raise ValueError("parameter name must be a simple lowercase identifier")
        if self.kind == "TEXT":
            if self.minimum is not None or self.maximum is not None or self.choices:
                raise ValueError("text parameter accepts max_chars only")
        elif self.kind == "INTEGER":
            if self.choices or self.max_chars is not None:
                raise ValueError("integer parameter accepts numeric bounds only")
            if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
                raise ValueError("integer parameter bounds are reversed")
        elif self.kind == "BOOLEAN":
            if self.choices or self.max_chars is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("boolean parameter has no extra constraints")
        elif self.kind == "CHOICE":
            if self.max_chars is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("choice parameter accepts options only")
            if not 2 <= len(self.choices) <= 20 or len(set(self.choices)) != len(self.choices):
                raise ValueError("choice parameter needs distinct options")
            if any(not x or len(x) > 128 for x in self.choices):
                raise ValueError("invalid choice")
        return self


class ProcedureStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str = Field(min_length=1, max_length=40)
    instruction: str = Field(min_length=1, max_length=800)
    executor: Executor
    success_test: str = Field(min_length=1, max_length=400)
    capability: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid(self):
        if not _IDENT.fullmatch(self.step_id):
            raise ValueError("step id must be a simple lowercase identifier")
        if not self.instruction.strip() or not self.success_test.strip():
            raise ValueError("step requires instruction and success test")
        if self.executor == "AGENT" and not self.capability:
            raise ValueError("agent step requires capability descriptor")
        if self.executor == "USER" and self.capability is not None:
            raise ValueError("user step cannot require agent tool authority")
        return self


class ProcedureDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=600)
    applicability: str = Field(min_length=1, max_length=600)
    limitations: str = Field(min_length=1, max_length=600)
    policy: ProcedurePolicy
    preconditions: tuple[str, ...] = Field(min_length=1, max_length=12)
    parameters: tuple[ParameterSpec, ...] = Field(default=(), max_length=12)
    steps: tuple[ProcedureStep, ...] = Field(min_length=1, max_length=20)
    success_tests: tuple[str, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def valid(self):
        if any(not text.strip() for text in (
            self.title, self.purpose, self.applicability, self.limitations,
            *self.preconditions, *self.success_tests,
        )):
            raise ValueError("procedure text cannot be blank")
        if any(len(x) > 400 for x in (*self.preconditions, *self.success_tests)):
            raise ValueError("procedure precondition/test too long")
        names = [p.name for p in self.parameters]
        ids = [s.step_id for s in self.steps]
        if len(set(names)) != len(names) or len(set(ids)) != len(ids):
            raise ValueError("duplicate parameter name or step id")
        if self.policy == "STABLE" and self.parameters:
            raise ValueError("stable procedure cannot vary parameters")
        if self.policy == "PARAMETERIZED" and not self.parameters:
            raise ValueError("parameterized procedure needs declared parameters")
        for step in self.steps:
            for text in (step.instruction, step.success_test):
                for name in _placeholders(text):
                    if name not in names:
                        raise ValueError("undeclared procedure parameter: " + name)
        return self


def _placeholders(template: str) -> tuple[str, ...]:
    names = []
    try:
        chunks = Formatter().parse(template)
        for _, name, fmt, conversion in chunks:
            if name is None:
                continue
            if not _IDENT.fullmatch(name) or fmt or conversion:
                raise ValueError("only simple {parameter} placeholders are supported")
            names.append(name)
    except ValueError as exc:
        raise ValueError("invalid procedure template placeholder") from exc
    return tuple(names)


class ProcedureUserCommand(GoalUserCommand):
    """Authenticated user turn, generation fencing, retry and action approval."""


class PublishSharedProcedureCommand(ProcedureUserCommand):
    template_id: UUID
    persona_id: UUID
    expected_current_version: int = Field(ge=0)
    definition: ProcedureDefinition
    review_note: str = Field(min_length=10, max_length=800)

    @model_validator(mode="after")
    def nonblank_review(self):
        if not self.review_note.strip():
            raise ValueError("shared publication requires a review note")
        return self


class CreatePersonalizedApproachCommand(ProcedureUserCommand):
    variant_id: UUID
    template_id: UUID
    template_version: int = Field(ge=1)
    goal_id: UUID | None = None
    strategy_id: UUID | None = None
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    step_text_overrides: dict[str, str] = Field(default_factory=dict)
    adaptation_reason: str | None = Field(default=None, min_length=10, max_length=800)

    @model_validator(mode="after")
    def goal_pair(self):
        if (self.goal_id is None) != (self.strategy_id is None):
            raise ValueError("goal and selected strategy must be supplied together")
        return self


class RevisePersonalizedApproachCommand(ProcedureUserCommand):
    variant_id: UUID
    expected_revision: int = Field(ge=1)
    template_version: int = Field(ge=1)
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    step_text_overrides: dict[str, str] = Field(default_factory=dict)
    adaptation_reason: str | None = Field(default=None, min_length=10, max_length=800)


class ProcedureChangeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_id: UUID
    revision: int = Field(ge=1)
    event_id: UUID
    action: Literal["SHARED_VERSION_PUBLISHED", "PERSONAL_VARIANT_CREATED", "PERSONAL_VARIANT_REVISED"]
    replayed: bool = False


class SharedProcedureView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    template_id: UUID
    persona_id: UUID
    version: int
    definition: ProcedureDefinition
    content_hash: str


class PersonalizedApproachHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    variant_id: UUID
    template_id: UUID
    template_version: int = Field(ge=1)
    revision: int = Field(ge=1)
    goal_id: UUID | None
    strategy_id: UUID | None
    title: str


class PersonalizedApproachView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    variant_id: UUID
    template_id: UUID
    template_version: int
    revision: int
    goal_id: UUID | None
    strategy_id: UUID | None
    parameters: dict[str, ParameterValue]
    step_text_overrides: dict[str, str]
    rendered_steps: tuple[ProcedureStep, ...]
    adaptation_reason: str | None
    definition_policy: ProcedurePolicy


def procedure_approval_phrase(command: ProcedureUserCommand) -> str:
    payload = command.model_dump(mode="json", exclude={
        "approval_turn_id", "idempotency_key", "expected_generation",
    })
    encoded = json.dumps({
        "command_type": type(command).__name__, "payload": payload,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "APPROVE PROCEDURE CHANGE " + hashlib.sha256(encoded).hexdigest()[:24].upper()


def validate_variant(
    definition: ProcedureDefinition,
    parameters: dict[str, ParameterValue],
    step_text_overrides: dict[str, str],
    adaptation_reason: str | None,
) -> tuple[ProcedureStep, ...]:
    """Deterministically validate/resolve instance parameters; no execution."""
    specs = {p.name: p for p in definition.parameters}
    if len(parameters) > 12 or set(parameters) - set(specs):
        raise ValueError("unknown/too many procedure parameters")
    for name, spec in specs.items():
        if name not in parameters:
            if spec.required:
                raise ValueError("missing required procedure parameter: " + name)
            continue
        value = parameters[name]
        if spec.kind == "TEXT":
            if type(value) is not str or not value.strip() or len(value) > (spec.max_chars or 256):
                raise ValueError("invalid text procedure parameter: " + name)
        elif spec.kind == "INTEGER":
            if type(value) is not int or (spec.minimum is not None and value < spec.minimum) or (spec.maximum is not None and value > spec.maximum):
                raise ValueError("invalid integer procedure parameter: " + name)
        elif spec.kind == "BOOLEAN":
            if type(value) is not bool:
                raise ValueError("invalid boolean procedure parameter: " + name)
        elif spec.kind == "CHOICE":
            if type(value) is not str or value not in spec.choices:
                raise ValueError("invalid choice procedure parameter: " + name)
    if definition.policy != "ADAPTIVE" and step_text_overrides:
        raise ValueError("only adaptive procedures allow private instruction changes")
    if step_text_overrides:
        if not adaptation_reason or not adaptation_reason.strip():
            raise ValueError("adaptive instruction change needs documented reason")
        if len(step_text_overrides) > 4:
            raise ValueError("adaptive procedure change budget exceeded")
        by_id = {step.step_id: step for step in definition.steps}
        for key, text in step_text_overrides.items():
            step = by_id.get(key)
            if step is None or step.executor != "USER":
                raise ValueError("can only adapt an existing user instruction")
            if not text.strip() or len(text) > 800:
                raise ValueError("invalid adaptive instruction")
            for name in _placeholders(text):
                if name not in specs:
                    raise ValueError("override uses undeclared parameter")
    result = []
    for step in definition.steps:
        source = step_text_overrides.get(step.step_id, step.instruction)
        # The string format only interpolates simple vetted keys; no property/index traversal.
        if any(name not in parameters for name in _placeholders(source)):
            raise ValueError("procedure instruction has missing parameter")
        if any(name not in parameters for name in _placeholders(step.success_test)):
            raise ValueError("procedure success test has missing parameter")
        rendered_instruction = source.format_map(parameters)
        rendered_test = step.success_test.format_map(parameters)
        if len(rendered_instruction) > 1200 or len(rendered_test) > 800:
            raise ValueError("rendered procedure text exceeds budget")
        result.append(step.model_copy(update={
            "instruction": rendered_instruction, "success_test": rendered_test,
        }))
    return tuple(result)


def procedure_content_hash(definition: ProcedureDefinition) -> str:
    canonical = json.dumps(definition.model_dump(mode="json"), sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
