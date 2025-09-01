from chatbot_state import ChatbotState, ProcessingTask
from workflow import Workflow
from util.cancellation_token import CancellationToken
from subprocess_manager import SubprocessManager


def main():
   ct = CancellationToken()
   state = ChatbotState(
      persona="assistant",
      history=[],
      user_input=None,
      output_message=None,
      stmMemory=ProcessingTask(name='stm', result=None, task=None, historyCheckpoint=0)
   )
   workflow = Workflow(cancellation_token=ct)
   try:
      sm = SubprocessManager()
      state = workflow.run(state, itearation_limit=5)
   except KeyboardInterrupt:
      ct.cancel()
   finally:
      del sm
   print(f"final state:\n{state}")
   for m in state['history']:
      print(m.content)


if __name__ == '__main__':
   main()
