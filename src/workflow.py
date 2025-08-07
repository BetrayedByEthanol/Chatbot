from langgraph.constants import START, END
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from nodes import *
from chatbot_state import ChatbotState
from util.cancellation_token import CancellationToken


class Workflow:
   graph: CompiledStateGraph
   _ct: CancellationToken

   def __init__(self, **kwargs):
      self._ct = kwargs.get("cancellation_token", CancellationToken())
      graph = StateGraph(ChatbotState)

      graph.add_node('generate_output', GenerateOutputNode.generate_output)

      graph.add_edge(START, 'generate_output')
      graph.add_edge('generate_output', END)

      self.graph = graph.compile()

   def run(self, state: ChatbotState, itearation_limit: int = 0) -> ChatbotState:
      iterations = 0
      while not self._ct.is_canceled() and not (itearation_limit <= iterations):
         state = self.graph.invoke(state, {"recursion_limit": 100})
         iterations += 1
      return state
