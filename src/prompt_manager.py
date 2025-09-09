import json
from datetime import datetime, timezone
import re
from math import exp
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate

CRITICAL_PREDICATES = {
   "goal", "deadline", "due_date", "decision", "project", "task",
   "repo", "environment", "api_key", "customer", "priority",
   "style", "persona", "constraint", "email", "phone", "timezone"
}


def make_slot_boosts(intent: dict | None) -> set[str]:
   if not intent:
      return set()
   return set(intent.get("missing_slots", []) or intent.get("slots_needed", []) or [])


def canon_value(v) -> str:
   return re.sub(r"\s+", " ", str(v).strip().lower())


def parcel_key(p: dict) -> tuple[str, str, str]:
   return (
      str(p.get("subject", "user")).lower(),
      str(p.get("predicate", "")).lower(),
      canon_value(p.get("value", "")),
   )


def parse_iso(ts: str | None) -> datetime | None:
   if not ts:
      return None
   try:
      dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
      return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
   except Exception:
      return None


def dedupe_parcels(parcels: list[dict]) -> list[dict]:
   """Merge identical parcels (same subj/pred/value). Keep max confidence/stability, sum support, latest last_seen."""
   merged: dict[tuple, dict] = {}
   for p in parcels:
      k = parcel_key(p)
      if k not in merged:
         # normalize last_seen
         ls = p.get("source", {}).get("timestamp")
         merged[k] = {
            **p,
            "support": max(1, int(p.get("support", 1))),
            "_last_seen_dt": parse_iso(ls),
            "last_seen": ls,
         }
      else:
         m = merged[k]
         m["support"] = m.get("support", 1) + max(1, int(p.get("support", 1)))
         m["confidence"] = max(float(m.get("confidence", 0)), float(p.get("confidence", 0)))
         m["stability"] = max(float(m.get("stability", 0)), float(p.get("stability", 0)))
         # latest last_seen
         ts = p.get("source", {}).get("timestamp")
         dt = parse_iso(ts)
         if dt and (m["_last_seen_dt"] is None or dt > m["_last_seen_dt"]):
            m["_last_seen_dt"] = dt
            m["last_seen"] = ts
   return list(merged.values())


def compute_salience(
      p: dict,
      *,
      now: datetime | None,
      recent_turns: set[int],
      turn_of_last_seen: int | None,
      slot_boosts: set[str]
) -> float:
   """Score: recency + support + confidence + criticality (+ intent slot boost)."""
   now = now or datetime.now(timezone.utc)
   support = float(min(max(1, int(p.get("support", 1))), 5)) / 5.0
   conf = float(p.get("confidence", 0.5))
   stab = float(p.get("stability", 0.5))
   pred = str(p.get("predicate", "")).lower()

   # recency by turns if we have them, else by time
   if turn_of_last_seen is not None and recent_turns:
      dist = max(0, max(recent_turns) - turn_of_last_seen)
      recency = exp(-dist / 6.0)  # half-life ~6 turns
      in_recent = 1.0 if turn_of_last_seen in recent_turns else 0.0
   else:
      dt = p.get("_last_seen_dt") or parse_iso(p.get("last_seen"))
      days = (now - dt).days if dt else 999
      recency = exp(-days / 14.0)  # half-life ~14 days
      in_recent = 0.0

   is_critical = 1.0 if pred in CRITICAL_PREDICATES else 0.0
   needs_slot = 1.0 if pred in slot_boosts else 0.0

   # Weighted sum (tune as you learn)
   return (
         0.32 * recency +
         0.23 * support +
         0.23 * conf +
         0.10 * stab +
         0.08 * is_critical +
         0.04 * needs_slot +
         0.04 * in_recent
   )


def index_ts_to_turn(history: list[dict]) -> dict[str, int]:
   """Map message timestamp -> absolute turn index (fallback to sequential ids)."""
   ts2turn: dict[str, int] = {}
   for i, m in enumerate(history):
      ts = m.get("metadata", {}).get("timestamp") or m.get("timestamp") or f"t{i}"
      ts2turn.setdefault(ts, i)
   return ts2turn


def map_last_seen_turn(p: dict, ts2turn: dict[str, int]) -> int | None:
   ts = p.get("last_seen") or p.get("source", {}).get("timestamp")
   return ts2turn.get(ts) if ts else None


def select_salient_from_stm(
      *,
      history: list[dict],
      parcels: list[dict],  # STM parcels
      k: int = 10,
      now: datetime | None = None,
      intent: dict | None = None
) -> list[dict]:
   """
   Return compact salient facts for the prompt:
   [{"key":"user:deadline","value":"2025-10-01","confidence":0.86,"last_seen":"..."}]
   """
   window = get_recent_window(history, k=8)  # your existing helper
   recent_turns = {m.get("turn", i) for i, m in enumerate(window)}
   ts2turn = index_ts_to_turn(history)
   slot_boosts = make_slot_boosts(intent)

   merged = dedupe_parcels(parcels)
   scored: list[tuple[float, dict]] = []
   for p in merged:
      t = map_last_seen_turn(p, ts2turn)
      s = compute_salience(p, now=now, recent_turns=recent_turns, turn_of_last_seen=t, slot_boosts=slot_boosts)
      scored.append((s, p))

   # keep only best per (subject:predicate) so we don’t show multiple values unless needed
   best_per_slot: dict[str, tuple[float, dict]] = {}
   for s, p in scored:
      slot = f"{str(p.get('subject', 'user')).lower()}:{str(p.get('predicate', '')).lower()}"
      if slot not in best_per_slot or s > best_per_slot[slot][0]:
         best_per_slot[slot] = (s, p)

   top = sorted(best_per_slot.values(), key=lambda x: x[0], reverse=True)[:k]

   return [{
      "key": f"{(p.get('subject') or 'user').lower()}:{str(p.get('predicate')).lower()}",
      "value": p.get("value"),
      "confidence": float(p.get("confidence", 0.5)),
      "last_seen": p.get("last_seen"),
   } for _, p in top]


REQ_PATTERNS = re.compile(
   r"\b(can you|could you|please|help me|how do i|what is|when is|where|why|should i|make|create|write|build|set|add|fix|debug)\b|[?]",
   re.IGNORECASE
)
RESOLVE_PATTERNS = re.compile(
   r"\b(done|completed|created|here (you|it) (go|are)|generated|fixed|updated|answer(ed)?|explained|implemented|summary|code block|link:)\b",
   re.IGNORECASE
)

# New: user acknowledges or cancels → close thread
ACK_PATTERNS = re.compile(r"\b(thanks|thank you|that works|perfect|got it|resolved|all good|cheers)\b", re.I)
CANCEL_PATTERNS = re.compile(r"\b(nevermind|no need|cancel|ignore that|forget it)\b", re.I)


def _is_user_interactive(msg: dict[str, Any]) -> bool:
   role = msg.get("role") or msg.get("type") or ""
   et = (msg.get("metadata") or {}).get("event_type", "user.text")
   return role == "user" and et.startswith("user.")


def _is_assistant(msg: dict[str, Any]) -> bool:
   role = msg.get("role") or msg.get("type") or ""
   return role == "assistant"


def _ts(msg: dict[str, Any]) -> str:
   return (msg.get("metadata") or {}).get("timestamp") or msg.get("timestamp") or ""


def _channel(msg: dict[str, Any]) -> str:
   return (msg.get("metadata") or {}).get("channel") or "text"


def _title_from(msg: dict[str, Any]) -> str:
   # Prefer first sentence from meaning; else first line; clip
   meaning = (msg.get("metadata") or {}).get("meaning") or {}
   sents = meaning.get("sentences") or []
   base = (sents[0] if sents else (msg.get("content") or "")).strip()
   base = base.split("\n")[0].strip()
   return (base[:140] + "…") if len(base) > 140 else base


def _intent_is_request(msg: dict[str, Any]) -> bool:
   intent = (msg.get("metadata") or {}).get("intent") or {}
   label = (intent.get("intent") or "").lower()
   # treat action/info/clarification as “threads”
   return label in {"action", "info", "clarification"}


def _missing_slots(msg: dict[str, Any]) -> list[str]:
   intent = (msg.get("metadata") or {}).get("intent") or {}
   return intent.get("missing_slots") or []


def extract_open_threads(history: list[dict[str, Any]], *, stale_after_turns: int = 12) -> list[dict[str, Any]]:
   """
   Returns open threads with rich metadata:
   [{"id","turn","title","status","since_turns","timestamp","channel","missing_slots"}]
   """
   # Build a simple list we can scan
   msgs = [(i, m) for i, m in enumerate(history)]

   # 1) Find candidate user requests
   candidates = []
   for i, m in msgs:
      if not _is_user_interactive(m):
         continue
      text = m.get("content") or ""
      is_req = bool(REQ_PATTERNS.search(text)) or _intent_is_request(m)
      if not is_req:
         continue
      title = _title_from(m)
      candidates.append({
         "id": f"thr-{i}",
         "turn": i,
         "title": title,
         "status": "open",
         "timestamp": _ts(m) or None,
         "channel": _channel(m),
         "missing_slots": _missing_slots(m) or [],
      })

   # 2) Walk forward to mark closures
   for c in candidates:
      opened_at = c["turn"]
      for j, m in msgs[opened_at + 1:]:
         # Assistant resolves explicitly
         if _is_assistant(m) and RESOLVE_PATTERNS.search(m.get("content") or ""):
            c["status"] = "closed"
            c["resolved_turn"] = j
            c["resolved_by"] = "assistant"
            break
         # User acknowledges or cancels
         if _is_user_interactive(m):
            txt = m.get("content") or ""
            if ACK_PATTERNS.search(txt):
               c["status"] = "closed"
               c["resolved_turn"] = j
               c["resolved_by"] = "user_ack"
               break
            if CANCEL_PATTERNS.search(txt):
               c["status"] = "closed"
               c["resolved_turn"] = j
               c["resolved_by"] = "user_cancel"
               break

   # 3) Only open; compute age
   open_threads = []
   last_turn = len(history) - 1
   now = datetime.now(timezone.utc).isoformat()
   for c in candidates:
      if c["status"] == "open":
         age = max(0, last_turn - c["turn"])
         c["since_turns"] = age
         c["stale"] = age >= stale_after_turns
         c.setdefault("timestamp", now)
         open_threads.append(c)

   return open_threads


def bullet(items):
   """
   items: list[str] OR list[dict with 'title' and optional status/missing_slots] -> bullet string.
   Adds subtle cues for missing slots and staleness.
   """
   if not items:
      return ""
   lines = []
   for it in items:
      if isinstance(it, dict):
         title = it.get("title", "(untitled)")
         suffix = []
         ms = it.get("missing_slots") or []
         if ms:
            suffix.append(f"missing: {', '.join(ms[:3])}")
         if it.get("stale"):
            suffix.append("stale")
         tail = f" — ({'; '.join(suffix)})" if suffix else ""
         lines.append(f"- {title}{tail}")
      else:
         lines.append(f"- {str(it)}")
   return "\n".join(lines)


def get_recent_window(history, k=16):
   return history[-k:]


def summarize_messages(summary):
   return summary


def extract_facts(messages):
   return []


# --- Base persona contract (always included) ---
BASE_CONTRACT = """You are a conversational assistant. Default style: snappy and witty; if the active persona demands it, be dry and sarcastic. Always keep it human-like and concise."""

# --- Audience blocks (pick exactly ONE per turn) ---
AUDIENCE_BLOCKS = {
   "user.text": """You are talking to a human via text. Be conversational and natural. Use short, flowing sentences. Bullets are fine only if they make things clearer. Don't ramble.""",
   "user.voice": """You are talking to a human via voice (your reply will be spoken aloud). Write in flowing, natural sentences that sound good when read out loud. Avoid bullet points or visual formatting.""",
   "system.event": """You are processing a system event, not chatting with a human. Do not banter. Only produce a short, neutral summary if it helps the user; otherwise output nothing.""",
   "admin.ops": """You are reporting to an operator. Be concise and technical, no humor. Focus on relevant status or errors only."""
}

# --- Channel tweaks (optional add-ons) ---
CHANNEL_HINTS = {
   "discord": "Light humor is okay; emojis sparingly if it fits the tone.",
   "terminal": "Keep it compact and plain text.",
   "browser": "Plain text; light structure only if requested.",
   "tts": "This will be spoken. Use natural prosody; no lists unless the user asked for steps."
}


def _pick_audience(event_type: str) -> str:
   if event_type.startswith("user.voice"):
      return "user.voice"
   if event_type.startswith("user."):
      return "user.text"
   if event_type in {"system.cron", "sensor.event", "tool.result", "system.signal"}:
      return "system.event"
   if event_type == "admin.cmd":
      return "admin.ops"
   # default to user text if unknown
   return "user.text"


def _clamp_chars(text: str, max_chars: int) -> str:
   if not text:
      return ""
   if len(text) > max_chars:
      return text[:max_chars] + "\n[...clipped...]"
   return text


def build_system_message(
      *,
      persona_name: str = "default",
      event_type: str = "user.text",
      channel: str = "browser",
      session_summary: str | None = None,
      salient_json: list[dict] | None = None,
      open_threads_bullets: str | None = None,
      meaning_json: dict | None = None,
      intent_json: dict | None = None,
      candidate_slot_fills: dict | None = None,
      now_iso: str | None = None,
      cap_summary: int = 1600,
      cap_meaning: int = 1600,
      cap_salient_items: int = 10,
) -> SystemMessage:
   """Returns a SystemMessage with only the relevant audience instructions injected."""

   TEMPLATE_PATH = Path("personas/assistant/base_template.md")
   template = TEMPLATE_PATH.read_text('utf-8')
   now_iso = now_iso or datetime.utcnow().isoformat() + "Z"
   # Clamp values before inject
   summary_val = _clamp_chars(session_summary, cap_summary) if session_summary else ""
   salient_val = ""
   if salient_json:
      compact = [{"k": x.get("key"), "v": x.get("value")} for x in salient_json[:cap_salient_items]]
      salient_val = json.dumps(compact, separators=(",", ":"))
   open_threads_val = open_threads_bullets or ""
   meaning_val = _clamp_chars(json.dumps(meaning_json, ensure_ascii=False), cap_meaning) if meaning_json else ""
   intent_val = json.dumps(intent_json, ensure_ascii=False) if intent_json else ""
   slot_fills_val = json.dumps(candidate_slot_fills, separators=(",", ":")) if candidate_slot_fills else ""

   # Audience block (inject only the relevant one)
   audience_key = _pick_audience(event_type)
   input_audience = audience_key
   input_channel = channel

   values = {
      "persona": persona_name,
      "input_audience": input_audience,
      "input_channel": input_channel,
      "session_summary": summary_val,
      "salient_json": salient_val,
      "open_threads_bullets": open_threads_val,
      "meaning_json": meaning_val,
      "intent_json": intent_val,
      "candidate_slot_fills_json": slot_fills_val,
      "now_iso": now_iso,
      "output_instructions": """- Default: conversational text.
- Use bullets/JSON **only if the user asked**"""
   }

   # Fill template
   content = template.format(**values)

   meta = {
      "kind": "chatbot_contract",
      "audience": audience_key,
      "channel": channel,
      "persona": persona_name,
      "tts_mode": (channel == "tts") or audience_key == "user.voice"
   }
   return SystemMessage(content=content, metadata=meta)


if __name__ == "__main__":
   history = [{"content": "messages", "role": "user"}, {"content": "message", "role": "assistant"}]
   recent = get_recent_window(history, k=8)
   summary = summarize_messages(history)  # keep one rolling summary elsewhere

   facts = extract_facts(history)  # your existing function
   salient = select_salient_from_stm(history=history, parcels=facts, k=10)

   mes = HumanMessage(
      content="text",
      metadata={
         "channel": "terminal",
         "timestamp": datetime.utcnow().isoformat() + "Z",
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
   summary = "Summary Text of last session or current session"

   sysmsg = build_system_message(
      persona_name="Persona",
      event_type=mes.metadata.get("event_type", "user.text"),
      channel=mes.metadata.get("channel", "browser"),
      session_summary=summary,
      salient_json=salient,
      open_threads_bullets=bullet(open_threads),
      meaning_json=mes.metadata.get("meaning"),
      intent_json=mes.metadata.get("intent"),
      now_iso=datetime.utcnow().isoformat(timespec='seconds') + "Z",
   )

   messages = [sysmsg, *history, mes]
   prompt = ChatPromptTemplate.from_messages(messages)
   print(prompt)
