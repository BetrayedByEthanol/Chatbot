from langchain_core.messages import HumanMessage

from src.chatbot_state import ChatbotState, InputSource
from src.model_manager import ModelManager


class UpdateHistoryNode:

   @staticmethod
   def update_history(state: ChatbotState) -> ChatbotState:
      if state['user_input'] and state['user_input'].source.name == InputSource.TEXT.name:
         state['history'].append(HumanMessage(content=state['user_input'].content))
         state['history'].append(state['output_message'])

      print(f'History: {len(state["history"])}')
      return state
