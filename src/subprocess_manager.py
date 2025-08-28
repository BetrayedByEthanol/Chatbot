import time
from asyncio import Queue, QueueEmpty
from concurrent.futures import ProcessPoolExecutor, Future
from threading import Thread, Event
from multiprocessing import Manager

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
      self.thread.start()
      self.tasks = Manager().dict()

   def __del__(self):
      self.stop.set()
      if self.thread:
         self.thread.join(2)
      if self.pool:
         self.pool.shutdown(cancel_futures=True)


   def _run(self):
      while not self.stop.is_set():
         try:
            task = self.queue.get_nowait()
            if task['name'] == "stm":
               fut = self.pool.submit(extract_facts, task['messages'])
               fut.add_done_callback(lambda f: self._done(f, task['name']))
         except QueueEmpty:
            time.sleep(10)

   @staticmethod
   def _done(future: Future, name: str):
      result: dict = future.result()
      print(name)
      for k, v in result.items():
         print(f"{k}: {v}")
         print(type(v))



if __name__ == '__main__':
   sm = SubprocessManager()
   sm.queue.put_nowait({"name": "stm", "messages": []})
   time.sleep(180)
   del sm
