from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel


@dataclass
class Parcel(BaseModel):
   ID: str
   Type: str
   Subject: str
   Predicate: str
   Value: str
   Datatype: str
   Confidence: float
   Stability: float
   Support: int
   TTL: dict[str, int]
   Evidence: str
   Source: dict[str, Any]
   Tags: list[str]
   Version: int
