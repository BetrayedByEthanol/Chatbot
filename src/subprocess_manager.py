import time
from asyncio import Queue, QueueEmpty
from concurrent.futures import ProcessPoolExecutor, Future
from threading import Thread, Event
from multiprocessing import Manager

from src.chatbot_state import ProcessingTask
from src.memory_manager import MemoryManager
from src.subprocesses.extract_facts import extract_facts


class SubprocessManagerMeta(type):
   _instances = {}

   @classmethod
   def __call__(mcs, *args, **kwargs):
      if mcs not in mcs._instances:
         instance = super().__call__(SubprocessManager, *args, **kwargs)
         mcs._instances[mcs] = instance
      return mcs._instances[mcs]


class SubprocessManager(metaclass=SubprocessManagerMeta):
   queue: Queue
   thread: Thread
   stop: Event
   pool: ProcessPoolExecutor
   tasks: dict

   def __init__(self):
      self.queue = Queue()
      self.stop = Event()
      self.pool = ProcessPoolExecutor(max_workers=1)
      self.thread = Thread(target=self._run, daemon=True)
      # self.tasks = Manager().dict()
      self.thread.start()

   def __del__(self):
      self.stop.set()
      if self.thread:
         self.thread.join(2)
      if self.pool:
         self.pool.shutdown(wait=True, cancel_futures=True)

   def queue_task(self, pt: ProcessingTask, history: list):
      self.queue.put_nowait({"name": pt.name, "messages": [message.content for message in history[pt.historyCheckpoint:] if message.type == "human"]})
      return ProcessingTask(task=None, result=None, name='stm', historyCheckpoint=len(history))

   def queue_output(self, out):
      ...


   def _run(self):
      futures = {}
      while not self.stop.is_set():
         try:
            task = self.queue.get_nowait()
            if task['name'] == "stm":
               fut = self.pool.submit(extract_facts, task['messages'])
               fut.add_done_callback(lambda f: self._done(f, task['name']))
               # future_id = id(fut)
               # futures[future_id] = fut
               # self.tasks[task['name']] = future_id
         except QueueEmpty:
            time.sleep(10)

   @staticmethod
   def _done(future: Future, name: str):
      result: list = future.result()
      MemoryManager().store_stm(memories=result)


if __name__ == '__main__':
   sm = SubprocessManager()
   sm.queue.put_nowait({"name": "stm", "messages": []})
   time.sleep(180)
   del sm
