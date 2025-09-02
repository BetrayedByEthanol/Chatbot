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
   STM: list[dict]

   def __init__(self):
      self.STM = []

   def store_stm(self, memories):
      self.STM.extend(memories)

   def summarize_memory(self):
      memories = self.STM
      lines = []
      for i, mem in enumerate(memories, 1):
         ctx = mem.get("context", {})
         prefs = mem.get("prefs", {})
         facts = mem.get("facts", [])
         flags = mem.get("flags", {})

         # Context
         ctx_bits = [f"{k}={v}" for k, v in ctx.items() if v]
         if ctx_bits:
            lines.append("Context: " + ", ".join(ctx_bits))

         # Preferences
         if prefs.get("likes") or prefs.get("dislikes"):
            likes = ", ".join(prefs.get("likes", []))
            dislikes = ", ".join(prefs.get("dislikes", []))
            if likes: lines.append("Likes: " + likes)
            if dislikes: lines.append("Dislikes: " + dislikes)

         # Facts
         for f in facts:
            line = f"- {f['predicate'].upper()} ({f.get('entity') or 'n/a'}): {f['value']} "
            line += f"(conf={f['confidence']:.2f}, stab={f['stability']:.2f}, seen={f['last_seen']})"
            fact_flags = [k for k, v in flags.items() if v]
            if fact_flags:
               line += " | Flags: " + ", ".join(fact_flags)
            lines.append(line)

      return "\n".join(lines)
