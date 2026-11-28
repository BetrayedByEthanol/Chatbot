"""Authenticated Governance control and host-only predictive evidence fences."""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,StrictBool


class LearningControl(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    user_id:UUID
    instance_id:UUID
    learning_enabled:bool=False
    version:int=Field(default=0,ge=0)
    reset_generation:int=Field(default=0,ge=0)
    reset_boundary_ref:UUID|None=None
    updated_at:datetime|None=None
    source_event_id:UUID|None=None


class LearningControlCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id:UUID
    expected_generation:int=Field(ge=1)
    expected_version:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=128)


class SetLearningEnabled(LearningControlCommand):
    enabled:StrictBool


class ResetLearnedState(LearningControlCommand):
    pass


class LearningControlResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    control:LearningControl
    changed:bool
    replayed:bool=False


@dataclass(frozen=True)
class PredictiveFence:
    """Host-captured exact sources and current control, never model claims.

    Only raw authenticated text sources are admitted by this increment.
    Later owners must pin every consumed input, including counterevidence.
    """
    control_version:int
    reset_generation:int
    source_turn_ids:tuple[UUID,...]
    historical_revision:int
    next_expiry:datetime|None
