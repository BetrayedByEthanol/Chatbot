"""Bounded cross-episode proposals; IDs refer only to supplied episode evidence."""
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator


class PatternProposal(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    topic: str=Field(min_length=1,max_length=80)
    hypothesis: str=Field(min_length=1,max_length=400)
    supporting_episode_ids: tuple[UUID,...]=Field(min_length=2,max_length=6)
    counter_episode_ids: tuple[UUID,...]=Field(default=(),max_length=4)

    @field_validator("topic","hypothesis")
    @classmethod
    def nonblank(cls,value):
        value=value.strip()
        if not value:
            raise ValueError("pattern text cannot be blank")
        return value

    @model_validator(mode="after")
    def distinct_evidence(self):
        refs=self.supporting_episode_ids+self.counter_episode_ids
        if len(refs)>6 or len(set(refs))!=len(refs):
            raise ValueError("pattern requires distinct, disjoint evidence, at most six episodes")
        return self


@dataclass(frozen=True)
class PatternEpisode:
    episode_id: UUID
    conversation_id: UUID
    source_turn_id: UUID
    source_turn_at: datetime
    topic: str
    description: str
    source_quote: str
    snapshot_hash: str


@dataclass(frozen=True)
class PatternHit:
    pattern_id: UUID
    version: int
    topic: str
    hypothesis: str
    model_id: str
    created_at: datetime
    supporting: tuple[PatternEpisode,...]
    counter: tuple[PatternEpisode,...]
    epistemic_status: Literal["MODEL_INTERPRETED"]="MODEL_INTERPRETED"


@dataclass(frozen=True)
class PatternRead:
    status: Literal["KNOWN","NO_EVIDENCE","BUDGET"]
    pattern_refs: tuple[tuple[UUID,int],...]=()
    source_turn_ids: tuple[UUID,...]=()


_PATTERN_QUESTION=re.compile(
    r"what (?:recurring )?patterns (?:have you noticed|have you observed|do you remember) "
    r"(?:about|in|with) (?:my )?(?P<topic>[^?!.\r\n]{1,100})\?",re.I,
)


def pattern_reference(text: str) -> str | None:
    """Recognize only standalone, explicitly topic-qualified pattern recall."""
    if not isinstance(text,str) or len(text)>180:
        return None
    match=_PATTERN_QUESTION.fullmatch(text.strip())
    if match is None:
        return None
    topic=match["topic"].strip()
    return topic or None
