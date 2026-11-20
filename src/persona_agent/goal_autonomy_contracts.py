"""MVP-03A.4: a bounded, non-authoritative Goal opportunity assessment."""
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .goal_contracts import GoalHorizon

GOAL_EVALUATION_OPERATION="goal.evaluate_decomposition.v1"
GOAL_EVALUATION_CAPABILITY="goal.evaluation"
GOAL_EVALUATION_RESOURCE="cognition.llm"
GOAL_EVALUATION_SOURCE="goal.autonomous.evaluator"

class GoalOpportunityCandidate(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    title:str=Field(min_length=1,max_length=120)
    desired_outcome:str=Field(min_length=1,max_length=600)
    success_criterion:str=Field(min_length=1,max_length=600)
    horizon:GoalHorizon
    contribution_note:str=Field(min_length=1,max_length=500)
    @model_validator(mode="after")
    def nonblank(self):
        if not all(v.strip() for v in (
            self.title,self.desired_outcome,self.success_criterion,
            self.contribution_note,
        )):
            raise ValueError("blank proposed goal")
        return self

class GoalOpportunityDraft(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    # No proposed work is an accepted Goal or execution instruction.
    candidate:GoalOpportunityCandidate|None=None

class GoalEvaluationTick(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    operation_id:UUID
    goal_id:UUID
    expected_generation:int=Field(ge=1)
    background_epoch:int=Field(ge=1)
    replayed:bool=False
