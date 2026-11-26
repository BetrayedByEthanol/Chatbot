"""Explicit supplied-source knowledge contracts. No fetching or execution."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator


class KnowledgeCommand(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    instance_id: UUID
    expected_generation: int=Field(ge=1)
    conversation_id: UUID
    evidence_turn_id: UUID
    evidence_quote: str=Field(min_length=1,max_length=300)
    approval_turn_id: UUID | None=None
    idempotency_key: str=Field(min_length=1,max_length=128)
    document_id: UUID

    @field_validator("evidence_quote")
    @classmethod
    def quote_not_blank(cls,value):
        if not value.strip() or "\x00" in value:
            raise ValueError("knowledge evidence quote is blank or invalid")
        return value


class ProcedureKnowledgeReference(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    variant_id: UUID
    revision: int=Field(ge=1)


class SaveKnowledgeCommand(KnowledgeCommand):
    expected_version: int=Field(ge=0)
    title: str=Field(min_length=1,max_length=160)
    source_kind: Literal["MANUAL","DOCUMENTATION","RESEARCH","USER_REFERENCE"]
    source_locator: str=Field(min_length=1,max_length=500)
    source_revision: str | None=Field(default=None,min_length=1,max_length=120)
    content: str=Field(min_length=1,max_length=24000)
    privacy_class: Literal["NORMAL","RESTRICTED"]
    expires_at: datetime | None=None
    procedure_refs: tuple[ProcedureKnowledgeReference,...]=Field(default=(),max_length=4)

    @field_validator("title","source_locator","source_revision")
    @classmethod
    def metadata(cls,value):
        if value is not None and (not value.strip() or any(ord(c)<32 or ord(c)==127 for c in value)):
            raise ValueError("knowledge metadata must be nonblank single-line text")
        return value

    @field_validator("content")
    @classmethod
    def body(cls,value):
        if not value.strip() or any(ord(c)<32 and c not in "\r\n\t" for c in value):
            raise ValueError("knowledge source must be nonblank text")
        return value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("knowledge expiry requires a timezone")
        if len({ref.variant_id for ref in self.procedure_refs})!=len(self.procedure_refs):
            raise ValueError("duplicate procedure reference")
        return self


class WithdrawKnowledgeCommand(KnowledgeCommand):
    expected_version: int=Field(ge=1)


class KnowledgeWriteResult(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    document_id: UUID
    version: int
    event_id: UUID
    status: Literal["ACTIVE","WITHDRAWN"]
    replayed: bool=False


@dataclass(frozen=True)
class KnowledgePassage:
    document_id: UUID
    version: int
    title: str
    source_kind: str
    source_locator: str
    source_revision: str | None
    content_sha256: str
    position: int
    start_offset: int
    end_offset: int
    passage_text: str
    recorded_at: datetime
    evidence_turn_id: UUID
    approval_turn_id: UUID
    epistemic_status: str="SUPPLIED_SOURCE"


def knowledge_approval_phrase(command: KnowledgeCommand) -> str:
    """Bind separate authenticated approval to the entire source and policy."""
    payload=command.model_dump(mode="json",exclude={"approval_turn_id","idempotency_key"})
    encoded=json.dumps({"type":type(command).__name__,"payload":payload},
                       ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")
    return "APPROVE KNOWLEDGE CHANGE "+hashlib.sha256(encoded).hexdigest()[:24].upper()


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def split_passages(content: str) -> tuple[tuple[int,int,str],...]:
    """Exact Unicode character offsets; never summarize or invent passages.

    Prefer whitespace boundaries in the last half of each bounded window.
    Non-whitespace source content is retained even for long unbroken lines.
    """
    if not 1<=len(content)<=24000 or not content.strip():
        raise ValueError("invalid source size")
    result=[]
    start=0
    while start<len(content):
        end=min(start+600,len(content))
        if end<len(content):
            boundary=max(content.rfind("\n",start+300,end),content.rfind(" ",start+300,end))
            if boundary>start:
                end=boundary+1
        raw=content[start:end]
        if raw.strip():
            left=start+len(raw)-len(raw.lstrip())
            right=start+len(raw.rstrip())
            result.append((left,right,content[left:right]))
        start=end
    return tuple(result)


_REFERENCE=re.compile(
    r"(?:what does (?:the )?saved (?:documentation|manual|knowledge) say about"
    r"|what do my saved (?:documents|manuals) say about)"
    r"\s+(?P<topic>[a-z0-9][a-z0-9 _-]{1,99})\?",re.I,
)


def knowledge_reference(text: str) -> str | None:
    """Explicit reference-memory lookup, not permission to execute a procedure."""
    if not isinstance(text,str) or len(text)>180:
        return None
    match=_REFERENCE.fullmatch(text.strip())
    return match["topic"].strip() if match else None
