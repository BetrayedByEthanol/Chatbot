from src.chatbot_state import ChatbotState
import select
import sys
from src.input_manager import InputManager



class InputNode:

   @staticmethod
   def get_input(state: ChatbotState) -> ChatbotState:
      im = InputManager()
      if im.process is None:
         im.simulate_external_input()
      print('>', end='')
      ready, _, _ = select.select([sys.stdin], [], [], 5)
      if ready:
         prompt = sys.stdin.readline().rstrip('\n')
         print(prompt)
      elif not im.queue.empty():
         print(im.queue.get_nowait())
      else:
         print('No input provided')
      return state
