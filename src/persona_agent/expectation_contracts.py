"""Bounded pilot contracts. Expectations are hypotheses about reports, not facts."""
from dataclasses import dataclass
from datetime import date,datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field

from .learning_control_contracts import PredictiveFence


@dataclass(frozen=True)
class ActivityReport:
    turn_id:UUID
    context_key:str
    occasion:date
    completed:bool


@dataclass(frozen=True)
class ExpectationPlan:
    context_key:str
    expected_version:int
    fence:PredictiveFence
    disposition:str
    maturity:str
    direction:str
    completion_occasions:int
    non_completion_occasions:int
    conflicting_occasions:int
    expires_at:datetime
    reports:tuple[ActivityReport,...]


class ExpectationView(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    revision_id:UUID
    reset_generation:int
    version:int
    context_key:str
    maturity:Literal["EARLY","TENTATIVE","CONFLICTED","UNAVAILABLE"]
    direction:Literal["COMPLETION","NON_COMPLETION","MIXED","UNKNOWN"]
    disposition:Literal["HYPOTHESIS","REVIEW_REQUIRED","WITHDRAWN"]
    completion_occasions:int
    non_completion_occasions:int
    conflicting_occasions:int
    source_turn_ids:tuple[UUID,...]
    expires_at:datetime|None
    occasion_evidence:tuple[ActivityReport,...]
    assessment_method:Literal["dated_activity_reports.v1"]="dated_activity_reports.v1"
    epistemic_status:Literal["HYPOTHESIS"]="HYPOTHESIS"
    evidence_class:Literal["USER_REPORTED"]="USER_REPORTED"


class WithdrawExpectation(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id:UUID
    revision_id:UUID
    expected_version:int=Field(ge=1)
    expected_generation:int=Field(ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)


class WithdrawalResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    revision_id:UUID
    version:int
    event_id:UUID
    replayed:bool=False
