"""MVP-03A.1: a deliberately narrow, deterministic delegated milestone contract.

This only proves a *weekly walking-frequency* child contributes to an approved
weekly walking-frequency parent. Arbitrary natural-language decomposition
requires owner review; model-provided prose is never proof of authorization.
"""
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeriveWeeklyWalkingMilestoneCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    goal_id: UUID
    parent_goal_id: UUID
    expected_parent_version: int = Field(ge=1)
    weekly_walks: int = Field(ge=1, le=7)


_NUMBER = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7,
}
_COUNT = r"(?:[1-7]|one|two|three|four|five|six|seven)"
_OUTCOME = re.compile(
    rf"^walk (?P<count>{_COUNT}) times? (?:a|per|each) week$",
    re.IGNORECASE,
)
_CRITERION = re.compile(
    rf"^(?:complete )?(?:at least )?(?P<count>{_COUNT}) "
    r"walks? (?:a|per|each) week$",
    re.IGNORECASE,
)


def weekly_walk_target(desired_outcome: str, success_criterion: str) -> int | None:
    """Only return a count when BOTH approved goal fields agree unambiguously."""
    outcome = _OUTCOME.fullmatch(" ".join(desired_outcome.split()))
    criterion = _CRITERION.fullmatch(" ".join(success_criterion.split()))
    if outcome is None or criterion is None:
        return None

    def number(raw: str) -> int:
        return int(raw) if raw.isdigit() else _NUMBER[raw.casefold()]

    left, right = number(outcome["count"]), number(criterion["count"])
    return left if left == right else None


def milestone_fields(weekly_walks: int) -> tuple[str, str, str]:
    """Construct all child goal content, not from untrusted model text."""
    word = "walk" if weekly_walks == 1 else "walks"
    times = "time" if weekly_walks == 1 else "times"
    return (
        f"Walk {weekly_walks} {times} per week",
        f"Walk {weekly_walks} {times} per week",
        f"Complete at least {weekly_walks} {word} per week",
    )
