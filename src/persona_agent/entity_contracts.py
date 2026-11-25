"""Memory-owned explicit entity commands; never model-issued mutations."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .entity_reference import EntityKind, EntityRelation, normalize_name


class EntityCommand(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    instance_id: UUID
    expected_generation: int=Field(ge=1)
    conversation_id: UUID
    evidence_turn_id: UUID
    idempotency_key: str=Field(min_length=1,max_length=128)


class RegisterEntityCommand(EntityCommand):
    name: str
    relation: EntityRelation

    @field_validator("name")
    @classmethod
    def validate_name(cls,value):
        normalize_name(value)
        return value.strip()


class AmendEntityCommand(EntityCommand):
    entity_id: UUID
    expected_version: int=Field(ge=1)
    action: Literal["CORRECT_RELATION","ADD_ALIAS","RETRACT"]
    relation: EntityRelation | None=None
    alias: str | None=None

    @model_validator(mode="after")
    def validate_change(self):
        if (self.action=="CORRECT_RELATION")!=(self.relation is not None):
            raise ValueError("only relation correction requires relation")
        if (self.action=="ADD_ALIAS")!=(self.alias is not None):
            raise ValueError("only alias addition requires alias")
        if self.alias is not None:
            normalize_name(self.alias)
            if self.alias!=self.alias.strip():
                raise ValueError("alias must not include surrounding whitespace")
        return self


class EntityWriteResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    entity_id: UUID
    version: int
    event_id: UUID
    replayed: bool=False


class EntityHit(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    entity_id: UUID
    entity_kind: EntityKind
    name: str
    relation: EntityRelation
    version: int
    relation_source_turn_id: UUID
    source_turn_at: datetime


class EntityConflict(Exception):
    """Source or requested identity revision does not support the command."""
