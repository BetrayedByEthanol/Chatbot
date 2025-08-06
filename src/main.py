from src.chatbot_state import ChatbotState
from workflow import Workflow
from util.cancellation_token import CancellationToken


def main():
   ct = CancellationToken()
   state = ChatbotState()
   workflow = Workflow(cancellation_token=ct)
   try:
      state = workflow.run(state)
   except KeyboardInterrupt:
      ct.cancel()
   print(f"final state:\n{state}")


if __name__ == '__main__':
   main()
