from uuid import uuid4

from langchain_core.messages import AIMessage

from src.chatbot_state import ChatbotState
from src.model_manager import ModelManager


class GenerateOutputNode:

   @staticmethod
   def generate_output(state: ChatbotState) -> ChatbotState:
      mm = ModelManager()
      response = mm.invoke(model_name='lexi',
                           persona=state['persona'],
                           template_name='base_template',
                           input_str=f"{state['user_input'].source.value}: {state['user_input'].content}",
                           history=state['history'],
                           stm_memory=state['stmMemory'].result)
      state['output_message'] = AIMessage(content=response)
      print(response)
      return state

   @staticmethod
   def get_recent_window(history, k=16):
      return history[-k:]

   @staticmethod
   def get_token_count(message: str):
      return int(len(message) / 3)

   @staticmethod
   def update_salient(turn_id, extracts):
      Salient: dict[str, dict[str, int]] = {}
      for k, v in extracts.items():
         if k not in Salient or Salient[k]["value"] != v:
            Salient[k] = {"value": v, "last_seen": turn_id}

   @staticmethod
   def add_thread(text, turn):
      OpenThreads: list[dict[str, str, int, str]] = [{"id": uuid4(), "text": text, "created": turn, "status": "open"}]

   @staticmethod
   def close_thread(thread_id):
      OpenThreads: list[dict[str, str, int, str]] = []
      next(t for t in OpenThreads if t["id"] == thread_id)["status"] = "closed"
