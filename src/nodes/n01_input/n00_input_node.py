from src.chatbot_state import ChatbotState, RawInput, InputSource
import select
import sys
from src.input_manager import InputManager



class InputNode:

   @staticmethod
   def get_input(state: ChatbotState) -> ChatbotState:
      im = InputManager()
      if im.process is None:
         im.simulate_external_input()
      ready, _, _ = select.select([sys.stdin], [], [], 0)
      if ready:
         sys.stdin.readline()
      print('>', end='')
      timeout = 15 if not im.queue.empty() else 60
      ready, _, _ = select.select([sys.stdin], [], [], timeout)
      if ready:
         state['user_input'] = RawInput(content=sys.stdin.readline().rstrip('\n'), source=InputSource.TEXT)
      elif not im.queue.empty():
         state['user_input'] = RawInput(content=im.queue.get_nowait(), source=InputSource.SUBPROCESS)
      else:
         state['user_input'] = RawInput(content='No input provided', source=InputSource.SYSTEM)
      return state
