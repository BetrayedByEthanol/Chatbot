from chatbot_state import ChatbotState
from workflow import Workflow
from util.cancellation_token import CancellationToken


def main():
   ct = CancellationToken()
   state = ChatbotState(persona="assistant", history=[], user_input=None, output_message=None)
   workflow = Workflow(cancellation_token=ct)
   try:
      state = workflow.run(state, itearation_limit=5)
   except KeyboardInterrupt:
      ct.cancel()
   print(f"final state:\n{state}")
   for m in state['history']:
      print(m.content)


if __name__ == '__main__':
   main()
