"""P4-06 in-app notifications: explicit user authority and platform-only delivery."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NotificationPolicyCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1,max_length=128)
    enabled: bool
    timezone: str = Field(min_length=1,max_length=100)
    quiet_start_min: int | None = Field(default=None,ge=0,le=1439)
    quiet_end_min: int | None = Field(default=None,ge=0,le=1439)

    @model_validator(mode="after")
    def complete_quiet_hours(self):
        if (self.quiet_start_min is None)!=(self.quiet_end_min is None):
            raise ValueError("quiet hours require start and end")
        return self


class ScheduleNotificationCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    intent_id: UUID
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=128)
    kind: Literal["REMINDER","FOLLOW_UP"]
    body: str = Field(min_length=1,max_length=500)
    due_at: datetime
    timezone: str = Field(min_length=1,max_length=100)
    recurrence: Literal["NONE","DAILY"] = "NONE"
    source_conversation_id: UUID | None = None
    source_turn_id: UUID | None = None

    @model_validator(mode="after")
    def valid_origin(self):
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        if (self.source_conversation_id is None)!=(self.source_turn_id is None):
            raise ValueError("conversation and turn must be paired")
        if not self.body.strip():
            raise ValueError("notification body cannot be blank")
        return self


class NotificationActionCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    occurrence_id: UUID
    idempotency_key: str = Field(min_length=1,max_length=128)
    action: Literal["ACK","STOP","SNOOZE","REPLY"]
    snooze_minutes: int | None = Field(default=None,ge=1,le=1440)
    reply_text: str | None = Field(default=None,min_length=1,max_length=1000)

    @model_validator(mode="after")
    def valid_action(self):
        if (self.action=="SNOOZE")!=(self.snooze_minutes is not None):
            raise ValueError("snooze requires an explicit bounded duration")
        if (self.action=="REPLY")!=(self.reply_text is not None):
            raise ValueError("reply requires text; other actions cannot carry text")
        if self.reply_text is not None and not self.reply_text.strip():
            raise ValueError("reply text cannot be blank")
        return self


class NotificationResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    intent_id: UUID
    event_id: UUID
    replayed: bool = False


class NotificationPolicyResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    version: int
    event_id: UUID
    replayed: bool = False


class NotificationActionResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    occurrence_id: UUID
    event_id: UUID
    replayed: bool = False


class InboxNotification(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    occurrence_id: UUID
    intent_id: UUID
    instance_id: UUID
    kind: Literal["REMINDER","FOLLOW_UP"]
    body: str
    scheduled_for: datetime
    available_at: datetime
