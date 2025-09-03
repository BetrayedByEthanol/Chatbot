import os
from langchain_core.language_models import BaseLLM
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langchain_core.output_parsers import BaseTransformOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser
from langchain_ollama.llms import OllamaLLM


class ModelManagerMeta(type):
   _instances = {}

   @classmethod
   def __call__(mcs, *args, **kwargs):
      if mcs not in mcs._instances:
         instance = super().__call__(ModelManager, *args, **kwargs)
         mcs._instances[mcs] = instance
      return mcs._instances[mcs]


class ModelManager(metaclass=ModelManagerMeta):
   avaible_models: dict[str, BaseLLM]
   parser: BaseTransformOutputParser
   templates: dict[(str, str), str]

   def __init__(self):
      self.parser = StrOutputParser()
      self.avaible_models = {'lexi': OllamaLLM(model="lexi")}
      self.templates = {}
      if os.path.exists('personas'):
         personas = os.listdir('personas')
         for persona in personas:
            template_files = os.listdir(f'personas/{persona}')
            for template_file in template_files:
               if '.' not in template_file:
                  continue
               template_name = template_file.split('.')[0]
               with open(f'personas/{persona}/{template_file}', 'r') as fp:
                  self.templates[(persona, template_name)] = fp.read()

   @staticmethod
   def format_prompt(prompt: str, stm_memory: str):
      prompt = prompt.replace("$STM_MEMORY$", stm_memory if stm_memory else "")
      return prompt

   def invoke(self, model_name: str, persona: str, template_name: str, input_str: str, history: list[BaseMessage], stm_memory: str) -> str:
      sys_prompt = self.templates[(persona, template_name)]
      sys_prompt = self.format_prompt(sys_prompt, stm_memory)
      prompt = ChatPromptTemplate.from_messages([SystemMessage(content=sys_prompt)] + history +
                                                [MessagesPlaceholder(variable_name="messages")])
      chain = prompt | self.avaible_models[model_name] | self.parser
      inputs = {"messages": [HumanMessage(content=input_str[input_str.index(':') + 1:])]}
      output = chain.invoke(inputs)
      return output
