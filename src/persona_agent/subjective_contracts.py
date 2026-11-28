"""Read-only subjective contribution and exact controlled-output pins."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from .learning_control_contracts import PredictiveFence


@dataclass(frozen=True)
class SubjectiveProjection:
    expectation_revision_id:UUID
    outcome_turn_id:UUID
    intention_id:UUID|None
    kind:Literal["MISMATCH","SUPPORT"]
    expires_at:datetime
    fence:PredictiveFence
    text:str
