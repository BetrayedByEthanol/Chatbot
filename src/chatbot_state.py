from enum import Enum
from typing import TypedDict
from dataclasses import dataclass
from concurrent.futures import Future
from langchain_core.messages import BaseMessage, AIMessage


@dataclass
class InputSource(Enum):
   TEXT = "TEXT"
   SYSTEM = "SYSTEM"
   SUBPROCESS = "Subprocess"


@dataclass
class RawInput:
   content: str
   source: InputSource


@dataclass
class ProcessingTask:
   name: str
   result: str | None
   task: Future | None | str
   historyCheckpoint: int


class ChatbotState(TypedDict):
   persona: str
   user_input: RawInput | None
   output_message: AIMessage | None
   history: list[BaseMessage]
   stmMemory: ProcessingTask
