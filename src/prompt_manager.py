import datetime
import json
import re
from collections import defaultdict
from math import exp

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

CRITICAL_PREDICATES = {
   "goal", "deadline", "due_date", "decision", "project", "task",
   "repo", "environment", "api_key", "customer", "priority",
   "style", "persona", "constraint"
}


def canon_value(v: str) -> str:
   return re.sub(r"\s+", " ", str(v).strip().lower())


def fact_key(f):
   return (str(f.get("entity") or "user").lower(),
           str(f.get("predicate")).lower(),
           canon_value(f.get("value")))


def dedupe_facts(facts):
   """Merge identical facts; count support and keep max confidence/stability & latest last_seen."""
   merged = {}
   for f in facts:
      k = fact_key(f)
      if k not in merged:
         merged[k] = {**f, "support": 1}
      else:
         m = merged[k]
         m["support"] += 1
         m["confidence"] = max(m.get("confidence", 0), f.get("confidence", 0))
         m["stability"] = max(m.get("stability", 0), f.get("stability", 0))
         if f.get("last_seen", "") > m.get("last_seen", ""):
            m["last_seen"] = f.get("last_seen")
   return list(merged.values())


def compute_salience(f, recent_window_turns: set[int], turn_of_last_seen: int | None):
   # Features
   support = f.get("support", 1)
   conf = f.get("confidence", 0.5)
   pred = str(f.get("predicate", "")).lower()
   is_critical_pred = 1.0 if pred in CRITICAL_PREDICATES else 0.0

   # Recency: if last_seen turn known, decay by distance; else neutral
   if turn_of_last_seen is not None:
      # closer = higher score; exp decay with half-life ~6 turns
      dist = 0 if len(recent_window_turns) == 0 else \
         max(0, max(recent_window_turns) - turn_of_last_seen)
      recency = exp(-dist / 6.0)
      in_recent = 1.0 if turn_of_last_seen in recent_window_turns else 0.0
   else:
      recency, in_recent = 0.5, 0.0

   # Simple weighted sum (tune weights as you like)
   score = (
         0.35 * recency +
         0.25 * (min(support, 5) / 5.0) +
         0.25 * conf +
         0.15 * is_critical_pred +
         0.10 * in_recent
   )
   return score


REQ_PATTERNS = re.compile(
   r"\b(can you|could you|please|help me|how do i|what is|when is|where|why|should i|make|create|write|build|set|add|fix|debug)\b|[?]",
   re.IGNORECASE
)
RESOLVE_PATTERNS = re.compile(
   r"\b(done|completed|created|here (you|it) (go|are)|generated|fixed|updated|answer(ed)?|explained|implemented|summary|code block|link:)\b",
   re.IGNORECASE
)


def index_turns(history):
   # Ensure each message has a 'turn' int; if not, assign sequentially.
   turns = {}
   for i, m in enumerate(history):
      turns.setdefault(m.get("timestamp") or f"t{i}", i)
   return turns  # map timestamp->turn idx


def map_fact_last_seen_turn(fact, ts_to_turn):
   ts = fact.get("last_seen")
   return ts_to_turn.get(ts) if ts else None


def select_session_critical_facts(history, facts, k=10):
   window = get_recent_window(history, k=8)
   recent_turns = {m.get("turn", i) for i, m in enumerate(window)}
   ts_to_turn = index_turns(history)

   merged = dedupe_facts(facts)
   scored = []
   for f in merged:
      t = map_fact_last_seen_turn(f, ts_to_turn)
      s = compute_salience(f, recent_turns, t)
      scored.append((s, f))

   # Top-K
   scored.sort(reverse=True, key=lambda x: x[0])
   top = [f for _, f in scored[:k]]

   # Return as tiny cache keyed by a stable slot name
   salient = []
   for f in top:
      salient.append({
         "key": f"{(f.get('entity') or 'user').lower()}:{str(f.get('predicate')).lower()}",
         "value": f.get("value"),
         "confidence": f.get("confidence", 0.5),
         "last_seen": f.get("last_seen", None)
      })
   return salient  # list of small dicts, easy to drop into prompt


def extract_open_threads(history):
   # Build a list of (turn, role, text)
   msgs = [(i, m.get("role", "user"), m.get("content", "")) for i, m in enumerate(history)]

   candidates = []
   for i, role, text in msgs:
      if role != "user":
         continue
      if REQ_PATTERNS.search(text):
         # crude title = first sentence / truncated
         title = text.strip().split("\n")[0]
         title = title[:140] + ("…" if len(title) > 140 else "")
         candidates.append({"id": f"thr-{i}", "turn": i, "title": title, "status": "open"})

   # Mark resolved if later assistant messages contain resolution cues
   for c in candidates:
      for j, role, text in msgs[c["turn"] + 1:]:
         if role != "assistant":
            continue
         if RESOLVE_PATTERNS.search(text):
            c["status"] = "closed"
            c["resolved_turn"] = j
            break

   open_threads = [c for c in candidates if c["status"] == "open"]
   return open_threads


def bullet(items):
   """items: list[str] OR list[dict with 'title'] -> single bullet string"""
   if not items:
      return ""
   if isinstance(items[0], dict):
      lines = [f"- {it.get('title', '(untitled)')}" for it in items]
   else:
      lines = [f"- {str(it)}" for it in items]
   return "\n".join(lines)


def get_recent_window(history, k=16):
   return history[-k:]


def summarize_messages(summary):
   return summary


def extract_facts(messages):
   return []


if __name__ == "__main__":
   history = [{"content": "messages", "role": "user"}, {"content": "message", "role": "assistant"}]
   recent = get_recent_window(history, k=8)
   summary = summarize_messages(history)  # keep one rolling summary elsewhere

   facts = extract_facts(history)  # your existing function
   salient = select_session_critical_facts(history, facts, k=10)

   mes = HumanMessage(
        content="text",
        metadata={
            "channel": "terminal",
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "user_id": "user",
            "attachments": [],
            "lang": 'en',
            "audio_ref": 'path',
            "turn": 'history len',
        },
    )
   open_threads = extract_open_threads(history)

   prompt_parts = [
      {"role": "system", "content": "persona + rules"},
      {"role": "system", "content": f"Session Summary:\n{summary}"},
      {"role": "system", "content": f"Salient Facts:\n{salient}"},
      {"role": "system", "content": f"Open Threads:\n{bullet(open_threads)}"},
      *recent
   ]

   prompt = ChatPromptTemplate.from_messages(prompt_parts)
   print(prompt)
   print(mes)
