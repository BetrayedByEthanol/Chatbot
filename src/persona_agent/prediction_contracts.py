"""Host-only, bounded prediction and comparison contracts (no action authority)."""
from dataclasses import dataclass
from datetime import date,datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict

from .learning_control_contracts import PredictiveFence

Route=Literal["COMPARE_NOW","DEFER","SKIP"]
Comparison=Literal["EXPECTED","UNEXPECTED","CONTRADICTORY","UNKNOWN"]


@dataclass(frozen=True)
class ComparisonRead:
    route:Route
    classification:Comparison|None=None
    reason:str=""
    expectation_revision_id:UUID|None=None
    outcome_turn_id:UUID|None=None
    fence:PredictiveFence|None=None
    expires_at:datetime|None=None


@dataclass(frozen=True)
class ForecastPlan:
    expectation_revision_id:UUID
    expectation_version:int
    commitment_turn_id:UUID
    context_key:str
    occasion:date
    expected_completion:bool
    expires_at:datetime
    fence:PredictiveFence


@dataclass(frozen=True)
class ComparisonPlan:
    read:ComparisonRead
    occasion:date
    completed:bool


@dataclass(frozen=True)
class ResolutionPlan:
    prediction_id:UUID
    expectation_revision_id:UUID
    outcome_turn_ids:tuple[UUID,...]
    resolution:Literal["HIT","MISS","INCONCLUSIVE"]
    comparison:Comparison
    expires_at:datetime
    fence:PredictiveFence


@dataclass(frozen=True)
class PredictionBatch:
    forecasts:tuple[ForecastPlan,...]=()
    comparisons:tuple[ComparisonPlan,...]=()
    resolutions:tuple[ResolutionPlan,...]=()

    def __bool__(self):
        return bool(self.forecasts or self.comparisons or self.resolutions)


class PredictionView(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    prediction_id:UUID
    expectation_revision_id:UUID
    expectation_version:int
    context_key:str
    target_occasion:date
    expected_completion:bool
    created_at:datetime
    expires_at:datetime
    state:Literal["PENDING","RESOLVED","HISTORICAL","EXPIRED"]
    resolution:Literal["HIT","MISS","INCONCLUSIVE"]|None=None
    comparison:Comparison|None=None
    outcome_turn_ids:tuple[UUID,...]=()
    evidence_class:Literal["USER_REPORTED"]="USER_REPORTED"
    # Diagnostic only. Later personalized projection requires new fences.
    epistemic_status:Literal["HYPOTHESIS"]="HYPOTHESIS"
