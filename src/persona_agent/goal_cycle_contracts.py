"""MVP-06C: a bounded recurring count outcome, not a completion counter.

Only explicit positive count measures with an independently checked
outcome/criterion pair can use this planner. Unsupported goals stay intact.
"""
from datetime import date,timedelta
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field

from .goal_decomposition_contracts import verified_count_measure

MAX_CYCLE_TARGET=12
# Do not convert a free-text verb or unit into a user commitment. Every
# supported action is deliberately one instance of the original count.
_ACTIONS={
    ("complete","walk"):"Complete one walk",
    ("run","run"):"Complete one run",
    ("complete","run"):"Complete one run",
    ("complete","exercise session"):"Complete one exercise session",
    ("complete","training session"):"Complete one training session",
    ("complete","workout"):"Complete one workout",
    ("complete","run session"):"Complete one running session",
    ("complete","cycle session"):"Complete one cycling session",
    ("complete","read session"):"Complete one reading session",
    ("complete","work out session"):"Complete one workout session",
    ("complete","practice piano session"):"Complete one piano practice session",
    ("read","book"):"Read one book",
    ("read","article"):"Read one article",
    ("read","chapter"):"Read one chapter",
    ("read","page"):"Read one page",
    ("prepare","meal"):"Prepare one meal",
    ("cook","meal"):"Cook one meal",
}


def recurring_count(outcome,criterion):
    measure=verified_count_measure(outcome,criterion)
    if (measure is None or measure[:2] not in _ACTIONS
            or not 1<=measure[3]<=MAX_CYCLE_TARGET):
        return None
    return measure


def cycle_bounds(period,day):
    """UTC calendar day, Monday-based week, or calendar month."""
    if period=="day":
        return day,day+timedelta(days=1)
    if period=="week":
        start=day-timedelta(days=day.weekday())
        return start,start+timedelta(days=7)
    if period=="month":
        start=day.replace(day=1)
        end=(start.replace(year=start.year+1,month=1) if start.month==12
             else start.replace(month=start.month+1))
        return start,end
    raise ValueError("unsupported cycle period")


class RecurringTaskDraft(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    goal_id:UUID
    expected_goal_version:int
    window_start:date
    window_end:date
    ordinal:int=Field(ge=1,le=MAX_CYCLE_TARGET)
    title:str
    action_description:str
    completion_criterion:str
    evidence_requirement:str
    executor:Literal["USER"]="USER"
    capability:None=None


def task_draft(goal_id,version,measure,start,end,ordinal):
    action=_ACTIONS[measure[:2]]
    window=f"{start.isoformat()} through {end.isoformat()} (UTC, end excluded)"
    return RecurringTaskDraft(
        goal_id=goal_id,expected_goal_version=version,window_start=start,
        window_end=end,ordinal=ordinal,title=f"{action} ({ordinal}/{measure[3]})",
        action_description=f"{action} during {window}.",
        completion_criterion=f"One distinct occurrence: {action.lower()} during {window}.",
        evidence_requirement=(
            "A distinct authenticated user DONE report for this occurrence, "
            "followed by explicit task confirmation; not independent verification."
        ),
    )


class RecurringOccurrenceView(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    ordinal:int
    task_id:UUID
    task_version:int
    task_status:str
    evidence_state:Literal["NONE","USER_REPORTED","USER_CONFIRMED","REQUIRES_REASSESSMENT",
                           "OUTSIDE_WINDOW","DUPLICATE_SOURCE"]
    report_id:UUID|None=None
    source_turn_id:UUID|None=None
    confirmation_id:UUID|None=None


class RecurringGoalView(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    goal_id:UUID
    goal_version:int
    goal_status:str
    desired_outcome:str
    success_criterion:str
    period:Literal["day","week","month"]|None=None
    unit:str|None=None
    target:int|None=None
    window_start:date|None=None
    window_end:date|None=None
    # Inclusive count of qualified reports, including confirmed reports.
    reported_count:int=0
    confirmed_count:int=0
    # No independently verified count admission in this USER-only slice.
    independently_verified_count:Literal[0]=0
    occurrences:tuple[RecurringOccurrenceView,...]=()
    decision:Literal["UNSUPPORTED","OUTSIDE_APPROVED_WINDOW","WINDOW_CLOSED","INACTIVE",
        "CHILDREN_OR_REVIEW","UNMAPPED_TASKS","EXCLUSIVE_OTHER","WAIT_FOR_EVIDENCE",
        "USER_CONFIRMED_TARGET_REPORTED_MET","BUDGET_EXHAUSTED","PROPOSE_ONE_TASK"]
    next_task:RecurringTaskDraft|None=None
