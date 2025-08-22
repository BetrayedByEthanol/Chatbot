from enum import Enum
from typing import TypedDict
from dataclasses import dataclass


@dataclass
class InputSource(Enum):
   TEXT = "TEXT"
   SYSTEM = "SYSTEM"
   SUBPROCESS = "Subprocess"


@dataclass
class RawInput:
   content: str
   source: InputSource


class ChatbotState(TypedDict):
   persona: str
   user_input: RawInput
