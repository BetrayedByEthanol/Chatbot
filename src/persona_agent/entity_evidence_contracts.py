"""Explicit entity-conditioned evidence and bounded directed relation contracts."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,model_validator

from .entity_contracts import EntityCommand

RelationKind=Literal["KNOWS","WORKS_WITH","WORKS_AT","LOCATED_AT"]
RELATION_LABELS={
    "KNOWS":"knows","WORKS_WITH":"works with",
    "WORKS_AT":"works at","LOCATED_AT":"is located at",
}
RELATION_KINDS={
    "KNOWS":("PERSON","PERSON"),"WORKS_WITH":("PERSON","PERSON"),
    "WORKS_AT":("PERSON","PLACE"),"LOCATED_AT":("OBJECT","PLACE"),
}


class LinkEntityEvidenceCommand(EntityCommand):
    entity_id: UUID
    expected_entity_version: int=Field(ge=1)
    source_type: Literal["USER_MEMORY","EPISODE"]
    source_id: UUID
    expected_memory_version: int | None=Field(default=None,ge=1)

    @model_validator(mode="after")
    def check_version(self):
        if (self.source_type=="USER_MEMORY")!=(self.expected_memory_version is not None):
            raise ValueError("only Memory evidence requires its revision")
        return self


class SetEntityRelationshipCommand(EntityCommand):
    subject_entity_id: UUID
    object_entity_id: UUID
    expected_subject_version: int=Field(ge=1)
    expected_object_version: int=Field(ge=1)
    expected_version: int=Field(ge=0)
    action: Literal["REGISTER","CORRECT","RETRACT"]
    relation: RelationKind | None=None

    @model_validator(mode="after")
    def check_action(self):
        if self.subject_entity_id==self.object_entity_id:
            raise ValueError("relationship endpoints must be distinct")
        if (self.action=="REGISTER")!=(self.expected_version==0):
            raise ValueError("registration expects an absent relationship")
        if (self.action=="RETRACT")!=(self.relation is None):
            raise ValueError("only retraction omits the new relation")
        return self


class EntityEvidenceResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    link_id: UUID
    event_id: UUID
    replayed: bool=False


class EntityRelationshipResult(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    relationship_id: UUID
    version: int
    event_id: UUID
    replayed: bool=False


class EntityRelationshipHit(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    relationship_id: UUID
    subject_entity_id: UUID
    object_entity_id: UUID
    subject_name: str
    object_name: str
    subject_relation: str
    object_relation: str
    relation: RelationKind
    version: int
    source_turn_id: UUID
    source_turn_at: datetime
