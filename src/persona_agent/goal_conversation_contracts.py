"""Bounded explicit goal requests, not a general intent classifier.

No model output can enter this grammar as evidence. Ordinary desires,
hypotheticals, approval of unrelated dialogue and compound requests abstain.
"""
from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field

APPROVE="Approve this goal."
REJECT="Don't save this goal."
ROUTE_ID="conversation.goal_candidate.v1"

@dataclass(frozen=True)
class GoalDialogueAction:
    kind: Literal["PROPOSE","REFINE","CRITERION","APPROVE","REJECT"]
    outcome: str | None=None
    criterion: str | None=None

_REQUEST=re.compile(
    r"(?P<prefix>(?:please )?(?:set|create|help me set) a goal to|"
    r"i want to (?:set|create) a goal to|my goal is to|"
    r"(?:please )?change (?:this|the) goal to) "
    r"(?P<outcome>[^.;!?\r\n\t]{1,120}?)"
    r"(?:; success means (?P<criterion>[^.;!?\r\n\t]{1,160}))?[.!]?",
    re.I,
)
_CRITERION=re.compile(r"for this goal, success means (?P<criterion>[^.;!?\r\n\t]{1,160})[.!]?",re.I)
_UNCERTAIN=re.compile(r"\b(?:maybe|perhaps|hypothetically|not sure|not a commitment|if|unless|but|or)\b",re.I)
# Multiple outcomes require separate user requests; no automatic splitting.
_COMPOUND=re.compile(r"\b(?:and|then|also)\b",re.I)
_WEEKLY=re.compile(
    r"(?P<activity>walk|exercise|run|cycle|read|work out|practice piano) "
    r"(?P<count>once|twice|(?:[1-7]|one|two|three|four|five|six|seven) times?) "
    r"(?:a|per|each) week",re.I,
)
_NUMBERS={"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
          "once":1,"twice":2}

def _weekly_fields(outcome):
    match=_WEEKLY.fullmatch(outcome)
    if match is None:
        return outcome,None
    activity=match["activity"].lower()
    raw=match["count"].lower().split()[0]
    count=int(raw) if raw.isdigit() else _NUMBERS[raw]
    times="time" if count==1 else "times"
    canonical=f"{activity.capitalize()} {count} {times} per week"
    if activity=="walk":
        from .goal_delegation_contracts import milestone_fields
        _,canonical,criterion=milestone_fields(count)
    else:
        criterion=f"Complete at least {count} {activity} session{'s' if count!=1 else ''} per week"
    return canonical,criterion

def parse_goal_dialogue(text: str) -> GoalDialogueAction | None:
    if not isinstance(text,str) or not 1<=len(text)<=300:
        return None
    value=text.strip().replace("’","'")
    if any(c in value for c in ("\r","\n","\t")):
        return None
    phrase=value.casefold().rstrip(".!")
    if phrase=="approve this goal":
        return GoalDialogueAction("APPROVE")
    if phrase=="don't save this goal" or phrase=="do not save this goal":
        return GoalDialogueAction("REJECT")
    criterion=_CRITERION.fullmatch(value)
    if criterion:
        content=criterion["criterion"].strip()
        if content and not _UNCERTAIN.search(content):
            return GoalDialogueAction("CRITERION",criterion=content)
        return None
    match=_REQUEST.fullmatch(value)
    if not match:
        return None
    outcome=match["outcome"].strip()
    criterion=match["criterion"].strip() if match["criterion"] else None
    if (not outcome or _UNCERTAIN.search(outcome) or _COMPOUND.search(outcome)
            or (criterion is not None and (not criterion or _UNCERTAIN.search(criterion)))):
        return None
    # Preserve exact user-supplied criteria. Normalize only the bounded
    # recurring target when no explicit criterion was supplied.
    if criterion is None:
        outcome,criterion=_weekly_fields(outcome)
    kind="REFINE" if "change " in match["prefix"].casefold() else "PROPOSE"
    return GoalDialogueAction(kind,outcome,criterion)


class GoalCandidateView(BaseModel):
    """Read-only proposal history; approval availability is not authority."""
    model_config=ConfigDict(extra="forbid",frozen=True)
    goal_id:UUID
    version:int=Field(ge=1,le=12)
    conversation_id:UUID
    selection_generation:int
    source_turn_id:UUID
    proposal_turn_id:UUID
    proposal_response_id:UUID
    desired_outcome:str
    success_criterion:str|None
    horizon:Literal["OPEN"]
    status:Literal["PENDING","SUPERSEDED","REJECTED","APPROVED"]
    decision_turn_id:UUID|None
    approval_available:bool
