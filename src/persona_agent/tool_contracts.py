"""Typed registered tool capabilities and scoped requests (P1-02)."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

TIME_TOOL="clock.now.v1"
SLOW_TOOL="text.statistics.v1"
PROCEDURE_AGENT_OPERATION="procedure.agent.text.statistics.v1"
RESOURCE_KEY="tool.readonly"


class ToolMode(str,Enum):
    IMMEDIATE="IMMEDIATE"
    FOREGROUND_ASYNC="FOREGROUND_ASYNC"


@dataclass(frozen=True)
class ToolCapability:
    name: str
    mode: ToolMode
    effect_class: Literal["READ_ONLY"]
    resource_key: str | None
    requires_authenticated_user: bool=True


CAPABILITIES={
    TIME_TOOL:ToolCapability(TIME_TOOL,ToolMode.IMMEDIATE,"READ_ONLY",None),
    SLOW_TOOL:ToolCapability(SLOW_TOOL,ToolMode.FOREGROUND_ASYNC,"READ_ONLY",RESOURCE_KEY),
}


class SlowTextToolCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    turn_id: UUID
    max_words: int=Field(default=5,ge=1,le=20)


class ToolCallResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    tool_call_id: UUID
    operation_id: UUID | None
    admission_state: Literal["PREPARING","QUEUED"]
    initial_reply_mode: Literal["DIRECT","DEFERRED"] | None
    followup_state: Literal["PENDING","READY","DEFERRED","SUPPRESSED","DELIVERED"]


class ToolFollowup(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    tool_call_id: UUID
    operation_id: UUID
    text: str
    replayed: bool=False


class ToolAuthorizationError(Exception):
    pass


class ToolResultNotReady(Exception):
    pass
