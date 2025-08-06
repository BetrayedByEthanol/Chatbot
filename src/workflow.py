from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from chatbot_state import ChatbotState
from util.cancellation_token import CancellationToken


class Workflow:
   graph: CompiledStateGraph
   _ct: CancellationToken
   _itearation_limit: int | None

   def __init__(self, **kwargs):
      self._itearation_limit = kwargs.get('max_iterations', None)
      self._ct = kwargs.get("cancellation_token", CancellationToken())
      graph = StateGraph(ChatbotState)

      self.graph = graph.compile()

   def run(self, state: ChatbotState, iterations: int = 0) -> ChatbotState:
      while not self._ct.is_canceled() and not (self._itearation_limit is not None and self._itearation_limit <= iterations):
         state = self.graph.invoke(state, {"recursion_limit": 100})
         iterations += 1
      return state
