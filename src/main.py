from src.chatbot_state import ChatbotState
from workflow import Workflow
from util.cancellation_token import CancellationToken
import os


def main():
   ct = CancellationToken()
   state = ChatbotState(persona="assistant")
   workflow = Workflow(cancellation_token=ct)
   try:
      state = workflow.run(state, itearation_limit=3)
   except KeyboardInterrupt:
      ct.cancel()
   print(f"final state:\n{state}")


if __name__ == '__main__':
   os.chdir('src')
   main()
