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


# Automatic acceptance covers explicitly enumerated countable units only.
# Never assume that arbitrary words following a number name counted objects:
# "4 hour marathons" might describe marathon duration, not four marathons.
# Unsupported units/qualifiers stay PENDING_REVIEW, even when parent and
# candidate strings appear structurally similar.
_WORD_NUMBER={"one":1,"two":2,"three":3,"four":4,"five":5,
              "six":6,"seven":7,"eight":8,"nine":9,"ten":10}
_AMOUNT=r"(?:[1-9][0-9]?|one|two|three|four|five|six|seven|eight|nine|ten)"
_MEASURE=re.compile(
    rf"^(?P<verb>[a-z]{{3,18}}) (?:at least )?(?P<count>{_AMOUNT}) "
    r"(?P<unit>[a-z][a-z -]{0,55}?) per (?P<period>day|week|month)$",
    flags=re.IGNORECASE,
)

# Explicit lexical normalization (not general singularization or semantic
# inference). Multiword units are admitted only by an audited phrase.
_COUNTABLE_UNITS={
    "book":"book", "books":"book",
    "article":"article", "articles":"article",
    "chapter":"chapter", "chapters":"chapter",
    "page":"page", "pages":"page",
    "walk":"walk", "walks":"walk",
    "run":"run", "runs":"run",
    "workout":"workout", "workouts":"workout",
    "meal":"meal", "meals":"meal",
    "lesson":"lesson", "lessons":"lesson",
    "session":"session", "sessions":"session",
    "exercise":"exercise", "exercises":"exercise",
    "meeting":"meeting", "meetings":"meeting",
    "visit":"visit", "visits":"visit",
    "marathon":"marathon", "marathons":"marathon",
    "recipe":"recipe", "recipes":"recipe",
    "exercise session":"exercise session",
    "exercise sessions":"exercise session",
    "training session":"training session",
    "training sessions":"training session",
}


def _measure(text: str) -> tuple[str,str,str,int] | None:
    """Normalize an unambiguous instance count; abstain for unknown phrases."""
    match=_MEASURE.fullmatch(" ".join(text.split()))
    if match is None:
        return None
    canonical_unit=_COUNTABLE_UNITS.get(match["unit"].casefold())
    if canonical_unit is None:
        return None
    count=match["count"].casefold()
    number=int(count) if count.isdigit() else _WORD_NUMBER[count]
    return (match["verb"].casefold(),canonical_unit,
            match["period"].casefold(),number)


def verified_count_measure(
    desired_outcome: str, success_criterion: str,
) -> tuple[str,str,str,int] | None:
    """Both fields must describe the identical, supported count measure."""
    outcome=_measure(desired_outcome)
    criterion=_measure(success_criterion)
    if outcome is None or criterion!=outcome:
        return None
    return outcome


def proven_count_milestone(
    parent_outcome: str,parent_criterion: str,
    child_outcome: str,child_criterion: str,
) -> bool:
    """Only accept an unambiguous, same-unit, lower-count milestone.

    Ambiguous duration/distance/property modifiers are not proof of
    authorized subgoal contribution, and require owner review.
    """
    parent=verified_count_measure(parent_outcome,parent_criterion)
    child=verified_count_measure(child_outcome,child_criterion)
    return (
        parent is not None and child is not None
        and parent[:3]==child[:3] and 1<=child[3]<parent[3]
    )


def proposal_digest(
    command: ProposeDerivedSubgoalCommand,
    *, accepted_measure: tuple[str,str,str,int] | None = None,
) -> str:
    """Canonical digest for proven counts, textual digest for review candidates.

    Title, horizon, words/digits, optional 'at least', plurality and
    whitespace cannot distinguish two otherwise identical accepted counts.
    Text for pending semantic review remains evidence, not a count proof.
    """
    if accepted_measure is not None:
        fields=("COUNT_MILESTONE_V1",*map(str,accepted_measure))
    else:
        fields=("REVIEW_TEXT_V1",
                " ".join(command.title.casefold().split()),
                " ".join(command.desired_outcome.casefold().split()),
                " ".join(command.success_criterion.casefold().split()),
                command.horizon)
    return sha256("\0".join(fields).encode("utf-8")).hexdigest()


class DerivedSubgoalProposalView(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    proposal_id: UUID
    proposed_goal_id: UUID
    parent_goal_id: UUID
    expected_parent_version: int=Field(ge=1)
    source_parent_event_id: UUID
    title: str
    desired_outcome: str
    success_criterion: str
    horizon: GoalHorizon
    contribution_note: str
    status: Literal["ACCEPTED","PENDING_REVIEW"]
    decision_method: Literal["DETERMINISTIC_COUNT_MILESTONE","REVIEW_REQUIRED"]
