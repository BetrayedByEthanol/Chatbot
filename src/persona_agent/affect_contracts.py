"""P2-07 typed domain contracts; no cognitive output may directly mutate state."""

from uuid import UUID
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

PreferenceKey=Literal[
    "RESPONSE_LENGTH","EMOJI_USE","PROACTIVE_MESSAGES","AVOID_PROACTIVE_TOPIC",
]


class SetRelationshipPreference(BaseModel):
    """Only authenticated user authority may set or revoke a boundary."""

    model_config=ConfigDict(extra="forbid",frozen=True)
    instance_id:UUID
    expected_generation:int=Field(ge=1)
    expected_version:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=128)
    conversation_id:UUID
    evidence_turn_id:UUID
    evidence_quote:str=Field(min_length=1,max_length=200)
    preference_key:PreferenceKey
    scope_key:str=Field(default="",max_length=80)
    preference_value:str=Field(min_length=1,max_length=80)
    action:Literal["SET","REMOVE"]="SET"

    @model_validator(mode="after")
    def valid_domain_value(self):
        scope=self.scope_key
        value=self.preference_value
        allowed={
            "RESPONSE_LENGTH":{"SHORT","NORMAL","DETAILED"},
            "EMOJI_USE":{"ON","OFF"},
            "PROACTIVE_MESSAGES":{"ALLOW","DISALLOW"},
            "AVOID_PROACTIVE_TOPIC":{"AVOID"},
        }
        if value not in allowed[self.preference_key]:
            raise ValueError("unsupported relationship preference value")
        if self.preference_key=="AVOID_PROACTIVE_TOPIC":
            if (not scope.strip() or scope!=scope.strip()
                or any(x in scope for x in ("\n","\r","<",">","{","}"))):
                raise ValueError("topic boundary requires a safe, nonblank scope")
        elif scope:
            raise ValueError("non-topic preferences may not carry a scope")
        return self


class RelationshipPreferenceResult(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    instance_id:UUID
    preference_key:PreferenceKey
    scope_key:str
    preference_value:str
    status:Literal["ACTIVE","REMOVED"]
    version:int=Field(ge=1)
    event_id:UUID
    replayed:bool=False
