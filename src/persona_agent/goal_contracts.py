"""P3-01 approved-goal and alternative-strategy contracts (v2.5 §21).

Goal -> Subgoal -> Strategy -> Procedure -> Task -> Operation remains distinct.
This increment provides user-authorized goal/strategy state and progress
evidence only. It does not define or execute procedures or automatically
declare real-world user actions verified.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator

GoalHorizon=Literal["SHORT","MEDIUM","LONG","OPEN"]
GoalStatus=Literal["ACTIVE","PAUSED","ABANDONED"]
EvidenceKind=Literal["PROGRESS","OBSTACLE","SUCCESS_REPORTED"]


class GoalUserCommand(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    instance_id:UUID
    expected_generation:int=Field(ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)
    conversation_id:UUID
    evidence_turn_id:UUID
    evidence_quote:str=Field(min_length=1,max_length=300)

    @field_validator("evidence_quote")
    @classmethod
    def quote_not_blank(cls,value:str)->str:
        if not value.strip():
            raise ValueError("goal evidence quote is blank")
        return value


class CreateGoalCommand(GoalUserCommand):
    goal_id:UUID
    parent_goal_id:UUID|None=None
    expected_parent_version:int|None=Field(default=None,ge=1)
    title:str=Field(min_length=1,max_length=120)
    desired_outcome:str=Field(min_length=1,max_length=600)
    success_criterion:str=Field(min_length=1,max_length=600)
    horizon:GoalHorizon
    target_at:datetime|None=None
    evaluation_start_at:datetime|None=None
    evaluation_end_at:datetime|None=None

    @model_validator(mode="after")
    def validate_hierarchy(self):
        if (self.parent_goal_id is None)!=(self.expected_parent_version is None):
            raise ValueError("subgoals require their parent's expected version")
        if self.parent_goal_id==self.goal_id:
            raise ValueError("a goal cannot be its own subgoal")
        for at in (self.target_at,self.evaluation_start_at,self.evaluation_end_at):
            if at is not None and (at.tzinfo is None or at.utcoffset() is None):
                raise ValueError("goal dates must include an explicit timezone")
        if (self.evaluation_start_at is not None
            and self.evaluation_end_at is not None
            and self.evaluation_end_at<=self.evaluation_start_at):
            raise ValueError("goal evaluation window must increase")
        if not all(x.strip() for x in (
            self.title,self.desired_outcome,self.success_criterion
        )):
            raise ValueError("goal title/outcome/success criterion cannot be blank")
        return self


class SetGoalStatusCommand(GoalUserCommand):
    goal_id:UUID
    expected_goal_version:int=Field(ge=1)
    new_status:GoalStatus


class RecordGoalEvidenceCommand(GoalUserCommand):
    goal_id:UUID
    expected_goal_version:int=Field(ge=1)
    evidence_kind:EvidenceKind
    evidence_id:UUID


class ProposeGoalStrategyCommand(GoalUserCommand):
    goal_id:UUID
    strategy_id:UUID
    title:str=Field(min_length=1,max_length=120)
    approach:str=Field(min_length=1,max_length=600)
    suitability_note:str=Field(min_length=1,max_length=400)

    @model_validator(mode="after")
    def nonblank(self):
        if not all(s.strip() for s in (
            self.title,self.approach,self.suitability_note
        )):
            raise ValueError("strategy fields cannot be blank")
        return self


class SelectGoalStrategyCommand(GoalUserCommand):
    goal_id:UUID
    strategy_id:UUID
    expected_goal_version:int=Field(ge=1)


class GoalCommandResult(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    goal_id:UUID
    goal_version:int=Field(ge=1)
    event_id:UUID
    action:Literal[
        "GOAL_CREATED","STATUS_CHANGED","EVIDENCE_RECORDED",
        "STRATEGY_PROPOSED","STRATEGY_SELECTED",
    ]
    entity_id:UUID
    replayed:bool=False


class GoalView(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    goal_id:UUID
    parent_goal_id:UUID|None
    depth:int=Field(ge=0,le=5)
    title:str
    desired_outcome:str
    success_criterion:str
    horizon:GoalHorizon
    status:GoalStatus
    version:int=Field(ge=1)
    selected_strategy_id:UUID|None
    evidence_count:int=Field(ge=0)


class StrategyView(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    strategy_id:UUID
    goal_id:UUID
    title:str
    approach:str
    suitability_note:str
    status:Literal["CANDIDATE"]


class EvidenceView(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    evidence_id:UUID
    goal_id:UUID
    kind:EvidenceKind
    epistemic_status:Literal["USER_REPORTED"]="USER_REPORTED"
    source_turn_id:UUID
    source_quote:str
