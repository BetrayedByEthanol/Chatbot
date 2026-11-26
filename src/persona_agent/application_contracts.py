"""Untrusted application input: explicit allowlist, no serialized actors."""
from typing import Annotated,Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,SecretStr,field_validator


class ApplicationRequest(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)


class SubmitText(ApplicationRequest):
    action:Literal["message"]
    text:str=Field(min_length=1,max_length=12000)
    idempotency_key:str=Field(min_length=1,max_length=128)

    @field_validator("text","idempotency_key")
    @classmethod
    def valid_text(cls,value):
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid input")
        return value


class ResumeText(ApplicationRequest):
    action:Literal["resume"]
    turn_id:UUID


class AcknowledgeText(ApplicationRequest):
    action:Literal["ack"]
    response_id:UUID


class ReadStatus(ApplicationRequest):
    action:Literal["status"]


class RecordPresence(ApplicationRequest):
    action:Literal["presence"]
    idempotency_key:str=Field(min_length=1,max_length=128)


    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls,value):
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid key")
        return value


class ApplicationEnvelope(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    token:SecretStr
    request:Annotated[
        SubmitText|ResumeText|AcknowledgeText|ReadStatus|RecordPresence,
        Field(discriminator="action"),
    ]
