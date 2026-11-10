"""Phase 0 identity-command contracts. Ingress must authenticate TrustedActor."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TrustedActor(BaseModel):
    """Only authenticated ingress may construct an actor for execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: UUID
    source_id: str = Field(min_length=1, max_length=128)


class RenameInstanceCommand(BaseModel):
    """Persona-runtime-owned metadata change, not a persona-definition edit."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    instance_id: UUID
    expected_generation: int = Field(ge=1)
    expected_version: int = Field(ge=0)
    new_label: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("new_label")
    @classmethod
    def normalize_label(cls, label: str) -> str:
        label = label.strip()
        if not label:
            raise ValueError("label must not be blank")
        return label


class RenameInstanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: UUID
    instance_id: UUID
    new_version: int
    new_label: str
    replayed: bool = False
