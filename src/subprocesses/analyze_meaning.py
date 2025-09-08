import re
from datetime import datetime, timedelta
import spacy

nlp = spacy.load("en_core_web_sm")

# --- Heuristic helpers (from your MVP, trimmed) ---
DATE_WORDS = {"today": 0, "tomorrow": 1, "day after tomorrow": 2, "eod": 0, "end of day": 0, "next week": 7}
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
URL_RE = re.compile(r"https?://\S+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(days?|hrs?|hours?|percent|%)?\b", re.I)
CMD_VERBS = r"\b(make|create|write|build|set|add|fix|debug|generate|summarize|explain|plan|design)\b"


def _rel_day_to_iso(offset: int, now: datetime) -> str:
   return (now + timedelta(days=offset)).date().isoformat()


def _extract_relative_dates(text: str, now: datetime) -> list[dict]:
   ents = []
   for word, off in DATE_WORDS.items():
      for m in re.finditer(rf"\b{re.escape(word)}\b", text, re.I):
         ents.append({"type": "DATE", "text": m.group(0), "norm": _rel_day_to_iso(off, now)})
   for wd, idx in WEEKDAYS.items():
      for m in re.finditer(rf"\b(next\s+{wd})\b", text, re.I):
         delta = (idx - now.weekday()) % 7
         delta = 7 if delta == 0 else delta
         ents.append({"type": "DATE", "text": m.group(0), "norm": _rel_day_to_iso(delta, now)})
   return ents


def _extract_numbers(text: str) -> list[dict]:
   out = []
   for m in NUM_RE.finditer(text):
      val = float(m.group(1));
      unit = m.group(2).lower() if m.group(2) else None
      if unit == "percent": unit = "%"
      out.append({"value": val, "unit": unit})
   return out


def _detect_questions(text: str) -> bool:
   return "?" in text or bool(re.search(r"\b(how|what|when|where|why|should)\b", text, re.I))


def _detect_commands(text: str) -> bool:
   return bool(re.search(CMD_VERBS, text, re.I)) or "please" in text.lower()


def identify_meaning(text: str, *, now: datetime | None = None, token_count_fn=lambda s: len(s.split())) -> dict:
   now = now or datetime.utcnow()
   t = re.sub(r"\s+", " ", text or "").strip()

   doc = nlp(t)

   # spaCy bits
   ents_spacy = [{"type": e.label_, "text": e.text} for e in doc.ents]
   noun_chunks = []
   seen = set()
   for nc in doc.noun_chunks:
      kp = " ".join(tok.lemma_.lower() for tok in nc if not tok.is_stop and tok.is_alpha)
      if kp and kp not in seen:
         noun_chunks.append(kp);
         seen.add(kp)
      if len(noun_chunks) >= 10: break

   # Heuristics augment/normalize
   ents = ents_spacy + _extract_relative_dates(t, now)
   # ensure EMAIL/URL if spaCy missed them
   ents += [{"type": "URL", "text": m.group(0)} for m in URL_RE.finditer(t)]
   ents += [{"type": "EMAIL", "text": m.group(0)} for m in EMAIL_RE.finditer(t)]

   nums = _extract_numbers(t)
   is_q = _detect_questions(t)
   is_cmd = _detect_commands(t)

   # Slots: first DATE/URL/EMAIL (you can add more later)
   def first(label):
      for e in ents:
         if e["type"] == label: return e.get("norm") or e["text"]

   slots = {}
   d, u, em = first("DATE"), first("URL"), first("EMAIL")
   if d:  slots["deadline"] = d
   if u:  slots["url"] = u
   if em: slots["email"] = em

   return {
      "text": doc.text,
      "lang": getattr(doc, "lang_", "en"),
      "tokens": token_count_fn(doc.text),
      "sentences": [s.text.strip() for s in doc.sents],
      "entities": ents,
      "keyphrases": noun_chunks,
      "numbers": nums,
      "questions": is_q,
      "commands": is_cmd,
      "critical_slots": slots,
   }
