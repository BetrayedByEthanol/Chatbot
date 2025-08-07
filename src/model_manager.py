import os
from langchain_core.language_models import BaseLLM
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import BaseTransformOutputParser
from langchain_core.prompts import PromptTemplate
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

   def invoke(self, model_name: str, persona: str, template_name: str, input_str: str) -> str:
      prompt = PromptTemplate.from_template(template=self.templates[(persona, template_name)])
      chain = prompt | self.avaible_models[model_name] | self.parser
      inputs = {"messages": [HumanMessage(content=input_str)]}
      output = chain.invoke(inputs)
      return output

