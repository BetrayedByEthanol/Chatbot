from typing import Any


class Parcel:
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
