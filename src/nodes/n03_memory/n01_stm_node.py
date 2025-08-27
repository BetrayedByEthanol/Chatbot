from __future__ import annotations
from langchain_core.messages import HumanMessage
from src.chatbot_state import ChatbotState
from typing import List, Dict, Optional, Callable
import json
import time
import hashlib
import redis
import re


# ---- Helpers ---------------------------------------------------------------

def _deep_merge(a: Dict, b: Dict) -> Dict:
   out = dict(a)
   for k, v in b.items():
      if isinstance(v, dict) and isinstance(out.get(k), dict):
         out[k] = _deep_merge(out[k], v)
      else:
         out[k] = v
   return out


def parse_session_slots(msg: str) -> dict:
   mode = "debug" if re.search(r"\bdebug\b", msg, re.I) else None
   m_step = re.search(r"\bstep\s*(\d+)\b", msg, re.I)
   step = int(m_step.group(1)) if m_step else None
   terse = bool(re.search(r"\b(terse|concise|short)\b", msg, re.I))
   task = None
   m_task = re.search(r"(?:fix(?:ing)?|working on)\s+(.{5,60}?)\.?$", msg, re.I)
   if m_task: task = m_task.group(1).strip()

   slots = {
      "mode": mode,
      "task": task,
      "step": step,
      "temp_prefs": {"style": "terse"} if terse else {},
      "scratch": {}
   }
   return {k: v for k, v in slots.items() if v not in (None, {}, [])}


def compute_msg_id(m: Dict) -> str:
   """
   Deterministic id for dedupe across workers.
   Include fields you care about; add tool_call_id/name as needed.
   """
   base = f"{m.get('role', '')}|{m.get('content', '')}|{m.get('name', '')}|{m.get('tool_call_id', '')}"
   return hashlib.sha256(base.encode("utf-8")).hexdigest()


# Atomic: dedupe (SET in seen set), append, trim, expire — all or nothing.
IDEMPOTENT_APPEND_LUA = """
-- KEYS[1] = list key, KEYS[2] = seen set key
-- ARGV[1] = msg_json, ARGV[2] = msg_id, ARGV[3] = max_messages, ARGV[4] = ttl
if redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 1 then return 0 end
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('RPUSH', KEYS[1], ARGV[1])
local maxm = tonumber(ARGV[3]) or 0
if maxm > 0 then redis.call('LTRIM', KEYS[1], -maxm, -1) end
local ttl = tonumber(ARGV[4]) or 0
if ttl > 0 then
  redis.call('EXPIRE', KEYS[1], ttl)
  redis.call('EXPIRE', KEYS[2], ttl)
end
return 1
"""


# ---- Storage ---------------------------------------------------------------

class RedisSTMStorage:
   """
   Redis-backed STM:
     - Messages stored as a Redis LIST per thread (lossless).
     - Idempotent appends via Lua + a per-thread SEEN set.
     - TTL & LTRIM guardrails.
     - Slots stored as a small JSON blob (merge-on-write).
     - Optional archive sink for durable logs (e.g., S3/SQL/file).
   """

   def __init__(
         self,
         url: str = "redis://localhost:6379/0",
         prefix: str = "stm:",
         ttl: Optional[int] = 24 * 3600,
         max_messages: int = 200,
         archive_sink: Optional[Callable[[List[Dict], str], None]] = None,
         decode_responses: bool = True,
   ):
      self.r = redis.from_url(url, decode_responses=decode_responses)
      self.prefix = prefix
      self.ttl = ttl
      self.max_messages = max_messages
      self.archive_sink = archive_sink
      self._append_script = self.r.register_script(IDEMPOTENT_APPEND_LUA)

   # --- Keys
   def k_messages(self, thread_id: str) -> str:
      return f"{self.prefix}msgs:{thread_id}"

   def k_seen(self, thread_id: str) -> str:
      return f"{self.prefix}seen:{thread_id}"

   def k_slots(self, thread_id: str) -> str:
      return f"{self.prefix}slots:{thread_id}"

   # --- Messages
   def append_messages_idempotent(self, thread_id: str, msgs: List[Dict]) -> int:
      """
      Append messages atomically with dedupe.
      Returns count of newly appended (non-duplicate) messages.
      """
      if not msgs:
         return 0
      list_key, seen_key = self.k_messages(thread_id), self.k_seen(thread_id)
      appended = 0
      now = time.time()
      appended_batch: List[Dict] = []
      for m in msgs:
         m = {**m, "ts": m.get("ts", now)}
         mid = m.get("id") or compute_msg_id(m)
         payload = json.dumps(m, separators=(",", ":"))
         res = self._append_script(
            keys=[list_key, seen_key],
            args=[payload, mid, self.max_messages or 0, self.ttl or 0]
         )
         if int(res) == 1:
            appended += 1
            appended_batch.append(m)

      if appended and self.archive_sink:
         try:
            self.archive_sink(appended_batch, thread_id)
         except Exception:
            pass

      return appended

   def load_messages(self, thread_id: str) -> List[Dict]:
      raw = self.r.lrange(self.k_messages(thread_id), 0, -1)
      return [json.loads(x) for x in raw]

   def clear_messages(self, thread_id: str) -> None:
      self.r.delete(self.k_messages(thread_id))
      self.r.delete(self.k_seen(thread_id))

   def merge_slots(self, thread_id: str, new_slots: Dict) -> None:
      if not new_slots:
         return
      key = self.k_slots(thread_id)

      current = self.get_slots(thread_id)
      merged = _deep_merge(current, new_slots)
      self.r.set(key, json.dumps(merged, separators=(",", ":")))
      if self.ttl:
         self.r.expire(key, self.ttl)

   def get_slots(self, thread_id: str) -> Dict:
      raw = self.r.get(self.k_slots(thread_id))
      return json.loads(raw) if raw else {}

   def clear_slots(self, thread_id: str) -> None:
      self.r.delete(self.k_slots(thread_id))

   def clear_all(self, thread_id: str) -> None:
      """Blow away messages, seen set, and slots for this thread."""
      self.r.delete(self.k_messages(thread_id), self.k_seen(thread_id), self.k_slots(thread_id))


class StmNode:
   @staticmethod
   def retrieve_stm(state: ChatbotState) -> ChatbotState:
      print(":)")
      return state


if __name__ == "__main__":
   red = RedisSTMStorage()
   message = HumanMessage(content="How are you?")
   usertext = " ".join(message.content for message in [message] if message.type == "human")
   red.merge_slots('thread-42', parse_session_slots(usertext))
   # red.append_messages_idempotent('thread-42', [message])
   #
   hist = red.load_messages('thread-42')
   print(hist)
   sl = red.get_slots("thread-42")
   print(sl)
