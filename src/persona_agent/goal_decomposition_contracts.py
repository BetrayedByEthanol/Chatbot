"""MVP-03A.2: bounded Goal-owned decomposition, not tool authorization.

The model/reflector may submit a typed candidate but cannot grant approval.
A mechanical numerical contribution may be accepted; everything else
becomes a non-actionable review candidate.
"""
import re
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .goal_contracts import GoalHorizon

class ProposeDerivedSubgoalCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id: UUID
    expected_generation: int=Field(ge=1)
    idempotency_key: str=Field(min_length=1,max_length=128)
    proposal_id: UUID
    proposed_goal_id: UUID
    parent_goal_id: UUID
    expected_parent_version: int=Field(ge=1)
    # Must reference the Goal owner's already-persisted creation event.
    source_parent_event_id: UUID
    title: str=Field(min_length=1,max_length=120)
    desired_outcome: str=Field(min_length=1,max_length=600)
    success_criterion: str=Field(min_length=1,max_length=600)
    horizon: GoalHorizon
    contribution_note: str=Field(min_length=1,max_length=500)

    @model_validator(mode="after")
    def validate_nonblank(self):
        if self.proposed_goal_id==self.parent_goal_id:
            raise ValueError("subgoal cannot be its own parent")
        if not all(x.strip() for x in (
            self.title,self.desired_outcome,self.success_criterion,
            self.contribution_note,
        )):
            raise ValueError("blank subgoal definition or contribution note")
        return self


class DerivedSubgoalProposalResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    proposal_id: UUID
    proposed_goal_id: UUID
    parent_goal_id: UUID
    status: Literal["ACCEPTED","PENDING_REVIEW"]
    decision_method: Literal["DETERMINISTIC_COUNT_MILESTONE","REVIEW_REQUIRED"]
    event_id: UUID
    accepted_goal_event_id: UUID | None = None
    replayed: bool = False


# A small, *proof-producing* class of generic measurable goals, not
# semantic similarity. Both approved parent fields and candidate fields
# must agree exactly on verb, singularized unit, period and count.
# Examples: "Read 3 books per month" or "Complete 4 exercise sessions per week".
_WORD_NUMBER={"one":1,"two":2,"three":3,"four":4,"five":5,
              "six":6,"seven":7,"eight":8,"nine":9,"ten":10}
_AMOUNT=r"(?:[1-9][0-9]?|one|two|three|four|five|six|seven|eight|nine|ten)"
_MEASURE=re.compile(
    rf"^(?P<verb>[a-z]{{3,18}}) (?:at least )?(?P<count>{_AMOUNT}) "
    r"(?P<unit>[a-z][a-z -]{0,55}?) per (?P<period>day|week|month)$",
    flags=re.IGNORECASE,
)


def _measure(text: str) -> tuple[str,str,str,int] | None:
    match=_MEASURE.fullmatch(" ".join(text.split()))
    if match is None:
        return None
    count=match["count"].lower()
    number=int(count) if count.isdigit() else _WORD_NUMBER[count]
    # A one-unit singular is treated as equivalent to its plural version,
    # not a semantic inference. Both strings must otherwise match.
    words=match["unit"].casefold().split()
    last=words[-1]
    if last.endswith("ies") and len(last)>3:
        words[-1]=last[:-3]+"y"
    elif last.endswith("s") and not last.endswith(("ss","us")) and len(last)>3:
        words[-1]=last[:-1]
    return (match["verb"].casefold()," ".join(words),
            match["period"].casefold(),number)


def proven_count_milestone(
    parent_outcome: str,parent_criterion: str,
    child_outcome: str,child_criterion: str,
) -> bool:
    """Fail closed except exact one-dimensional, same-period count reductions.

    A strict quantitative reduction creates only a *supporting milestone*.
    No claim of user completion, task eligibility or tool permission follows.
    """
    p1,p2=_measure(parent_outcome),_measure(parent_criterion)
    c1,c2=_measure(child_outcome),_measure(child_criterion)
    if None in (p1,p2,c1,c2) or p1!=p2 or c1!=c2:
        return False
    return p1[:3]==c1[:3] and 1<=c1[3]<p1[3]


def proposal_digest(command: ProposeDerivedSubgoalCommand) -> str:
    """Deduplicate equivalent candidate definitions on the same parent."""
    fields=(command.title.strip().casefold(),
            command.desired_outcome.strip().casefold(),
            command.success_criterion.strip().casefold(),
            command.horizon)
    return sha256("\0".join(fields).encode("utf-8")).hexdigest()
