"""Read-only subjective contribution and exact controlled-output pins."""
from dataclasses import dataclass
from datetime import date,datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field

from .learning_control_contracts import PredictiveFence


@dataclass(frozen=True)
class SubjectiveProjection:
    expectation_revision_id:UUID
    outcome_turn_id:UUID
    occasion:date
    intention_id:UUID|None
    kind:Literal["MISMATCH","SUPPORT"]
    expires_at:datetime
    fence:PredictiveFence
    text:str


class CancelSubjectiveIntention(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id:UUID
    intention_id:UUID
    expected_generation:int=Field(ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)


class IntentionCancellationResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    intention_id:UUID
    event_id:UUID
    replayed:bool=False
