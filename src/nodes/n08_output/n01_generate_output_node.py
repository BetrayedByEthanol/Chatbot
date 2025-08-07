from src.chatbot_state import ChatbotState
from src.model_manager import ModelManager


class GenerateOutputNode:

   @staticmethod
   def generate_output(state: ChatbotState) -> ChatbotState:
      mm = ModelManager()
      response = mm.invoke(model_name='lexi',persona=state['persona'],template_name='base_template',input_str="Greetings!")
      print(response)
      return state
