import re
from datetime import datetime, timedelta

DATE_WORDS = {
   "today": 0, "tomorrow": 1, "day after tomorrow": 2,
}
CMD_VERBS = r"\b(make|create|write|build|set|add|fix|debug|generate|summarize|explain|plan|design)\b"

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s?(am|pm)?)\b", re.I)
MONEY_RE = re.compile(r"(\$|€)\s?\d+(?:[\.,]\d+)?")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://\S+")
NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(days?|hrs?|hours?|percent|%)?\b", re.I)


def normalize_text(s: str) -> str:
   return re.sub(r"\s+", " ", s).strip()


def split_sentences(s: str) -> list[str]:
   parts = re.split(r"(?<=[.!?])\s+", s)
   return [p.strip() for p in parts if p.strip()]


def rel_day_to_iso(offset: int) -> str:
   return (datetime.utcnow() + timedelta(days=offset)).date().isoformat()


def extract_entities(text: str) -> list[dict]:
   ents = []
   # relative dates
   for word, off in DATE_WORDS.items():
      for m in re.finditer(rf"\b{re.escape(word)}\b", text, re.I):
         ents.append({"type": "DATE", "text": m.group(0), "norm": rel_day_to_iso(off)})
   # absolute dates
   for m in DATE_RE.finditer(text):
      raw = m.group(0)
      norm = raw
      try:
         if "-" in raw:
            norm = datetime.fromisoformat(raw).date().isoformat()
         elif "/" in raw:
            mm, dd, yy = raw.split("/")
            yy = int(yy);
            yy = yy if yy > 99 else (2000 + yy)
            norm = datetime(int(yy), int(mm), int(dd)).date().isoformat()
      except Exception:
         pass
      ents.append({"type": "DATE", "text": raw, "norm": norm})
   for m in TIME_RE.finditer(text):
      ents.append({"type": "TIME", "text": m.group(0), "norm": m.group(1)})
   for m in MONEY_RE.finditer(text):
      ents.append({"type": "MONEY", "text": m.group(0)})
   for m in EMAIL_RE.finditer(text):
      ents.append({"type": "EMAIL", "text": m.group(0)})
   for m in URL_RE.finditer(text):
      ents.append({"type": "URL", "text": m.group(0)})
   return ents


def extract_keyphrases(text: str) -> list[str]:
   # super light: keep 1–3 word lowercase spans containing letters/digits/_
   words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", text)
   chunks = []
   buf = []
   for w in words:
      if len(buf) < 3:
         buf.append(w.lower())
      if len(buf) == 3:
         chunks.append(" ".join(buf));
         buf = []
   if buf: chunks.append(" ".join(buf))
   # de-dup
   seen, out = set(), []
   for c in chunks:
      if c not in seen:
         out.append(c);
         seen.add(c)
   return out[:10]


def extract_numbers(text: str) -> list[dict]:
   out = []
   for m in NUM_RE.finditer(text):
      val = float(m.group(1))
      unit = m.group(2).lower() if m.group(2) else None
      if unit == "percent": unit = "%"
      out.append({"value": val, "unit": unit})
   return out


def detect_questions(text: str) -> bool:
   return "?" in text or bool(re.search(r"\b(how|what|when|where|why|should)\b", text, re.I))


def detect_commands(text: str) -> bool:
   return bool(re.search(CMD_VERBS, text, re.I)) or bool(re.search(r"\bplease\b", text, re.I))


CRITICAL_SLOTS = {
   "deadline": lambda ents: next((e["norm"] for e in ents if e["type"] == "DATE"), None),
   "email": lambda ents: next((e["text"] for e in ents if e["type"] == "EMAIL"), None),
   "url": lambda ents: next((e["text"] for e in ents if e["type"] == "URL"), None),
}


def identify_meaning(text: str, lang: str = "en", token_count_fn=lambda s: len(s.split())) -> dict:
   t = normalize_text(text)
   sents = split_sentences(t)
   ents = extract_entities(t)
   nums = extract_numbers(t)
   keys = extract_keyphrases(t)
   q = detect_questions(t)
   cmd = detect_commands(t)
   slots = {k: f(ents) for k, f in CRITICAL_SLOTS.items() if f(ents)}
   return {
      "text": t,
      "lang": lang,
      "tokens": token_count_fn(t),
      "sentences": sents,
      "entities": ents,
      "keyphrases": keys,
      "numbers": nums,
      "questions": q,
      "commands": cmd,
      "critical_slots": slots,
   }

import spacy
nlp = spacy.load("en_core_web_sm")  # or "de_core_news_md" for German
# Optional: add an EntityRuler for domain terms
from spacy.pipeline import EntityRuler
ruler = nlp.add_pipe("entity_ruler", before="ner")
ruler.add_patterns([
    {"label":"REPO", "pattern":[{"TEXT":{"REGEX":"^[a-z0-9-_]+/[a-z0-9-_]+$"}}]},
    {"label":"ENV",  "pattern":"production"}
])

def identify_meaning_spacy(text: str, token_count_fn=lambda s: len(s.split())) -> dict:
    doc = nlp(text.strip())
    # Entities (convert to your shape)
    ents = [{"type": ent.label_, "text": ent.text} for ent in doc.ents]
    # Noun-chunk keyphrases (clean & cap)
    keys = []
    seen = set()
    for nc in doc.noun_chunks:
        kp = " ".join(t.lemma_.lower() for t in nc if not t.is_stop and t.is_alpha)
        if kp and kp not in seen:
            keys.append(kp); seen.add(kp)
        if len(keys) >= 10: break
    # Questions/commands (basic)
    is_q = any(t.text == "?" for t in doc) or any(t.lemma_.lower() in {"how","what","when","where","why","should"} for t in doc)
    # Imperative-ish: root VERB at start or “please”
    root = [t for t in doc if t.head == t][0] if doc else None
    is_cmd = any(t.lower_ == "please" for t in doc) or (root and root.pos_ == "VERB" and doc[0].pos_ == "VERB")

    # Slots: pick first DATE/URL/EMAIL from ents (you can normalize dates as before)
    def first_ent(label):
        for e in ents:
            if e["type"] == label: return e["text"]
    slots = {}
    d = first_ent("DATE"); u = first_ent("URL"); em = first_ent("EMAIL")
    if d: slots["deadline"] = d
    if u: slots["url"] = u
    if em: slots["email"] = em

    return {
        "text": doc.text,
        "lang": doc.lang_ if hasattr(doc, "lang_") else "en",
        "tokens": token_count_fn(doc.text),
        "sentences": [s.text.strip() for s in doc.sents],
        "entities": ents,
        "keyphrases": keys,
        "numbers": [],  # keep your NUM regex if you want
        "questions": is_q,
        "commands": is_cmd,
        "critical_slots": slots,
    }
