from src.chatbot_state import ChatbotState
from src.model_manager import ModelManager


class GenerateOutputNode:

   @staticmethod
   def generate_output(state: ChatbotState) -> ChatbotState:
      mm = ModelManager()
      response = mm.invoke(model_name='lexi',
                           persona=state['persona'],
                           template_name='base_template',
                           input_str=f"{state['user_input'].source.value}: {state['user_input'].content}")
      print(response)
      return state
