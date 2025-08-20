from multiprocessing import Queue, Process


class InputManagerMeta(type):
   _instances = {}

   @classmethod
   def __call__(mcs, *args, **kwargs):
      if mcs not in mcs._instances:
         instance = super().__call__(InputManager, *args, **kwargs)
         mcs._instances[mcs] = instance
      return mcs._instances[mcs]


class InputManager(metaclass=InputManagerMeta):
   queue: Queue
   process: Process | None

   def __init__(self):
      self.queue = Queue()
      self.process = None

   def simulate_external_input(self):
      self.process = Process(target=external_input, args=(self.queue,))
      self.process.start()


def external_input(queue: Queue):
   import time
   for i in range(5):
      time.sleep(12)
      queue.put("simulated external input")
