from transformers import pipeline

HF_MODEL_ID = "Falconsai/intent_classification"
_intent = pipeline("text-classification", model=HF_MODEL_ID, top_k=None)

LABEL_MAP = {
   "information_request": "info",
   "instruction": "action",
   "clarification": "clarification",
   "opinion": "opinion",
   "chit_chat": "chit_chat",
}


def analyze_intent(text: str, meaning: dict) -> dict:
   t = (text or "").strip()
   if not t:
      return {"intent": "other", "confidence": 0.0, "reason": "empty", "slots": {}}

   # Heuristics first (cheap)
   if meaning.get("questions"):
      heuristic = ("info", 0.7, "heuristic:question")
   elif meaning.get("commands"):
      heuristic = ("action", 0.7, "heuristic:command")
   else:
      heuristic = ("other", 0.5, "heuristic:default")

   # HF classifier (strong labeler)
   out = _intent(t, truncation=True)[0]  # list of {label, score}
   top = max(out, key=lambda r: r["score"])
   mapped = LABEL_MAP.get(top["label"], "other")
   conf = float(top["score"])

   # Ensemble: if HF is strong (>=0.80), trust it; else let heuristic sway
   if conf >= 0.80:
      intent, confidence, reason = mapped, conf, f"falcon:{top['label']}"
   else:
      # If heuristic and HF agree, boost; else favor heuristic for routing but keep HF note
      if mapped == heuristic[0]:
         intent = mapped
         confidence = min(1.0, (conf + heuristic[1]) / 2 + 0.1)
         reason = f"falcon+{heuristic[2]}"
      else:
         intent, confidence, reason = heuristic[0], max(conf, heuristic[1] - 0.05), f"heuristic_over_falcon:{top['label']}"

   # Nudge question mark into info if still ambiguous
   if "?" in t and intent not in {"info", "clarification"} and confidence < 0.8:
      intent, confidence, reason = "info", max(confidence, 0.7), reason + "+qm_nudge"

   return {
      "intent": intent,
      "confidence": confidence,
      "reason": reason,
      "slots": meaning.get("critical_slots", {})
   }
