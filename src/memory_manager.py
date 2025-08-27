from concurrent.futures import ProcessPoolExecutor

from src.subprocesses.extract_facts import extract_facts


class MemoryManagerMeta(type):
   _instances = {}

   @classmethod
   def __call__(mcs, *args, **kwargs):
      if mcs not in mcs._instances:
         instance = super().__call__(MemoryManager, *args, **kwargs)
         mcs._instances[mcs] = instance
      return mcs._instances[mcs]


class MemoryManager(metaclass=MemoryManagerMeta):

   @staticmethod
   def extract_memory(self, memory_type: str, history):
      if memory_type == 'stm':
         with ProcessPoolExecutor() as ex:
            fut = ex.submit(extract_facts, ([],))
