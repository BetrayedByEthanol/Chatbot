import uuid
from typing import Optional, List, Dict, Literal, Union, Any, Iterable, Tuple
from pydantic import BaseModel, Field
from uuid import UUID


class Persona(BaseModel):
   name: str = Field("personaname", json_schema_extra={"mutant": False})


class PersonaConfig(BaseModel):
   length: int = Field(15, json_schema_extra={"mutant": True})


class User(BaseModel):
   ID: UUID = uuid.uuid4()
   persona: PersonaConfig = PersonaConfig()
   value: str = Field("name", json_schema_extra={"mutant": True})
   vayette: str = Field("nom", json_schema_extra={})
   instructions_session: str
   instructions_day: str
   short_term_goal: str
   mid_term_goal: str
   long_term_goal: str
   session_start: str
   last_session_end: str
   time_limit: str


def extract(ob):
   fields = ob.__class__.model_fields
   for k, v in fields.items():
      if hasattr(v, "json_schema_extra") and isinstance(v.json_schema_extra, dict):
         print(f"Yes to {k}: {getattr(ob, k)} ({v.json_schema_extra.get('mutant', False)})")
      elif issubclass(v.annotation, BaseModel):
         xx = getattr(user, k)
         extract(xx)


if __name__ == "__main__":
   user = User(ID=uuid.uuid4())
   extract(user)
