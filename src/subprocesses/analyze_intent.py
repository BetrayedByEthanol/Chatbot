from typing import Literal, Optional
from langchain_core.messages import BaseMessage
from transformers import pipeline

HF_MODEL_ID = "Falconsai/intent_classification"  # plug-and-play
_intent_clf = pipeline("text-classification", model=HF_MODEL_ID, top_k=None)

RequestType = Literal["info", "action", "clarification", "opinion", "chit_chat", "other"]

LABEL_MAP = {
   # map HF labels to your internal set
   "information_request": "info",
   "instruction": "action",
   "clarification": "clarification",
   "opinion": "opinion",
   "chit_chat": "chit_chat",
}


from transformers import pipeline
from langchain_core.messages import BaseMessage

HF_MODEL_ID = "Falconsai/intent_classification"
_intent_pipe = pipeline("text-classification", model=HF_MODEL_ID, top_k=None)

LABEL_MAP = {
    # HF → your internal intents
    "information_request": "info",
    "instruction": "action",
    "clarification": "clarification",
    "opinion": "opinion",
    "chit_chat": "chit_chat",
}

FALLBACK_DEFAULT = {"intent":"other","confidence":0.0,"reason":"empty_or_non_user","slots":{}}

def analyze_intend_falcon(msg: BaseMessage, meaning: dict) -> dict:
    et = (msg.metadata or {}).get("event_type", "user.text")
    if not et.startswith("user."):
        return FALLBACK_DEFAULT  # skip cron/tools/etc.

    text = (msg.content or "").strip()
    if not text:
        return FALLBACK_DEFAULT

    out = _intent_pipe(text, truncation=True)[0]  # list of {label, score}
    top = max(out, key=lambda r: r["score"])
    mapped = LABEL_MAP.get(top["label"], "other")
    probs = {r["label"]: float(r["score"]) for r in out}

    # Heuristic nudge using your Step 2 meaning:
    if ("?" in text) and mapped not in {"info","clarification"} and top["score"] < 0.80:
        mapped = "info"

    return {
        "intent": mapped,
        "confidence": float(top["score"]),
        "reason": f"falcon:{top['label']}",
        "probs": probs,
        "slots": (meaning or {}).get("critical_slots", {}),
    }


def fuse_intents(primary: dict, regex_signal: dict | None) -> dict:
   if not regex_signal:
      return primary
   if primary["confidence"] >= 0.80:
      return primary  # strong win

   # If regex is clearly stronger, adopt it
   if regex_signal["confidence"] > primary["confidence"] + 0.15:
      return {**primary, "intent": regex_signal["type"], "confidence": max(primary["confidence"], regex_signal["confidence"]),
              "reason": primary["reason"] + "+regex"}

   # Otherwise just temper confidence
   if primary["intent"] != regex_signal["type"]:
      primary["confidence"] = min(primary["confidence"], 0.79)
      primary["reason"] += "+regex_disagree"
   else:
      primary["confidence"] = min(1.0, (primary["confidence"] + regex_signal["confidence"]) / 2 + 0.1)
      primary["reason"] += "+regex_agree"
   return primary
