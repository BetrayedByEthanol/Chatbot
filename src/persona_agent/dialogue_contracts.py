"""P1-03 typed, evidence-linked dialogue state contracts.

These records track what was discussed/asked, not goal or procedure success.
Explicit caller proposals require authenticated scope and a conversation
revision. A plain 'okay' only resolves an ACKNOWLEDGMENT, never permission.
"""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Attention(str, Enum):
    FOCUSED="FOCUSED"
    RECENT="RECENT"
    DORMANT="DORMANT"


class Resolution(str, Enum):
    OPEN="OPEN"
    RESOLVED="RESOLVED"
    ABANDONED="ABANDONED"


class DialogueActKind(str, Enum):
    QUESTION="QUESTION"
    DECISION="DECISION"
    ACKNOWLEDGMENT="ACKNOWLEDGMENT"


class DialogueOutcome(str, Enum):
    ANSWERED="ANSWERED"
    CONFIRMED="CONFIRMED"
    DECLINED="DECLINED"


class DialogueBaseCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")

    conversation_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    evidence_turn_id: UUID
    idempotency_key: str = Field(min_length=1,max_length=128)


class OpenThreadCommand(DialogueBaseCommand):
    thread_id: UUID
    title: str = Field(min_length=1,max_length=160)


class FocusThreadCommand(DialogueBaseCommand):
    thread_id: UUID


class SetThreadProgressCommand(DialogueBaseCommand):
    thread_id: UUID
    resolution: Resolution
    attention: Attention
    next_conversational_action: str | None=Field(default=None,max_length=200)


class OfferDialogueActCommand(DialogueBaseCommand):
    thread_id: UUID
    act_id: UUID
    source_response_id: UUID
    kind: DialogueActKind
    label: str = Field(min_length=1,max_length=200)


class ResolveDialogueActCommand(DialogueBaseCommand):
    act_id: UUID
    outcome: DialogueOutcome | None
    cancel: bool=False

    @field_validator("outcome")
    @classmethod
    def _valid_outcome(cls, value):
        return value

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):
        result=super().model_validate(obj,*args,**kwargs)
        if result.cancel == (result.outcome is not None):
            raise ValueError("provide an outcome or cancel, not both or neither")
        return result


class RegisterReferentCommand(DialogueBaseCommand):
    thread_id: UUID
    referent_id: UUID
    kind: str = Field(pattern="^(TOPIC|ENTITY)$")
    label: str = Field(min_length=1,max_length=160)


class DialogueChangeResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    conversation_id: UUID
    thread_id: UUID
    revision: int
    event_id: UUID
    replayed: bool=False


class PendingAct(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    act_id: UUID
    thread_id: UUID
    kind: DialogueActKind
    label: str
    source_response_id: UUID


class Referent(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    referent_id: UUID
    thread_id: UUID
    kind: str
    label: str
    source_turn_id: UUID


class DialogueThread(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    thread_id: UUID
    title: str
    attention_status: Attention
    resolution_status: Resolution
    next_conversational_action: str | None
    revision: int


class ActiveDialogue(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    conversation_id: UUID
    instance_id: UUID
    revision: int
    focus_thread_id: UUID | None
    threads: tuple[DialogueThread,...]
    pending_acts: tuple[PendingAct,...]
    referents: tuple[Referent,...]
