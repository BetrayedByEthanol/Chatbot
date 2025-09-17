import datetime
import pathlib
from collections import defaultdict

from ollama import chat
from langchain_core.output_parsers import JsonOutputParser


def extract_facts(messages: list):
   parser = JsonOutputParser()
   pth = pathlib.Path(__file__).parent
   with open(str(pth) + '/prompts/extract_facts.md', 'r') as fp:
      prompt = fp.read()

   tools = [{
      "type": "function",
      "function": {
         "name": "emit_parcels_draft",
         "description": "Extract user-stated facts/preferences/goals/etc. from the CURRENT user message only.",
         "parameters": {
            "type": "object",
            "properties": {
               "context": {
                  "type": "object",
                  "properties": {
                     "mode": {"type": ["string", "null"]},
                     "task": {"type": ["string", "null"]},
                     "step": {"type": ["integer", "null"]}
                  },
                  "additionalProperties": False
               },
               "flags": {
                  "type": "object",
                  "properties": {
                     "awaiting_user_data": {"type": "boolean"},
                     "needs_clarification": {"type": "boolean"},
                     "memory_conflict": {"type": "boolean"},
                     "high_confidence_update": {"type": "boolean"}
                  },
                  "required": ["awaiting_user_data", "needs_clarification", "high_confidence_update"],
                  "additionalProperties": False
               },
               "scratch": {
                  "type": "object",
                  "properties": {
                     "reasoning": {"type": ["string", "null"]},
                     "skipped_items": {"type": ["string", "null"]}
                  },
                  "additionalProperties": False
               },
               "parcels_draft": {
                  "type": "array",
                  "maxItems": 5,
                  "items": {
                     "type": "object",
                     "properties": {
                        "type": {"type": "string", "enum": ["preference", "fact", "profile", "event", "task", "rule", "summary", "mood", "identity"]},
                        "predicate": {"type": "string"},
                        "value": {},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "stability": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "subject": {"type": "string"}
                     },
                     "required": ["type", "predicate", "value", "confidence", "stability", "evidence"],
                     "additionalProperties": False
                  }
               }
            },
            "required": ["context", "flags", "scratch", "parcels_draft"],
            "additionalProperties": False
         }
      }
   }]

   examples = [
      {
         "role": "user",
         "content": "Can you teach me some opening chess moves?"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": "teach", "step": 1},
                  "parcels_draft": [
                     {
                        "type": "task",
                        "predicate": "goal",
                        "value": "chess",
                        "confidence": 0.70,
                        "stability": 0.30,
                        "subject": "user",
                        "tags": ["boardgame", "beginner", 'education'],
                        "evidence": "Can you teach me some opening chess moves?"
                     }
                  ],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }, {
         "role": "user",
         "content": "okay"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_2",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "parcels_draft": [],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }, {
         "role": "user",
         "content": "It's cold outside"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_3",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "parcels_draft": [
                     {
                        "type": "event",
                        "predicate": "condition",
                        "value": "cold outside",
                        "confidence": 0.95,
                        "stability": 0.30,
                        "subject": "user",
                        "tags": ["weather"],
                        "evidence": "cold outside"
                     }
                  ],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }, {
         "role": "user",
         "content": "Chocolate ice cream is yummy"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_4",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "parcels_draft": [
                     {
                        "type": "preference",
                        "predicate": "likes",
                        "value": "chocolate ice cream",
                        "confidence": 0.95,
                        "stability": 0.9,
                        "subject": "user",
                        "tags": ["food"],
                        "evidence": "Chocolate ice cream is yummy"
                     }
                  ],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }, {
         "role": "user",
         "content": "Do you want to play chess?"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_5",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": 'play chess', "step": 1},
                  "parcels_draft": [
                     {
                        "type": "task",
                        "predicate": "play",
                        "value": "play chess",
                        "confidence": 0.95,
                        "stability": 0.9,
                        "subject": "user",
                        "tags": ["boardgame", "tool use"],
                        "evidence": "Do you want to play chess?"
                     }
                  ],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }, {
         "role": "user",
         "content": "It's raining outside and I do not like the rain"
      },
      {
         "role": "assistant",
         "tool_calls": [{
            "id": "call_6",
            "type": "function",
            "function": {
               "name": "emit_parcels_draft",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "parcels_draft": [
                     {"type": "event", "predicate": "weather", "value": "raining", "confidence": 0.9, "stability": 0.3, "evidence": "It’s raining outside", "tags": ["weather"], "subject": "user"},
                     {"type": "preference", "predicate": "dislikes", "value": "rain", "confidence": 0.9, "stability": 0.7, "evidence": "I do not like the rain", "tags": ["weather"], "subject": "user"}
                  ],
                  "flags": {"awaiting_user_data": False, "needs_clarification": False, "high_confidence_update": False},
                  "scratch": {"reasoning": None, "skipped_items": None}
               }
            }
         }]
      }
   ]
   messages = ['I think pizza is delicous ', "It's raining outside", "My favorite pets are cats.", 'yes go ahead', "Do you want to play poker", "I understand", "I prefer python", "i just came from outside, and man i do hate snow!"]
   # messages = ['I think pizza is delicous ', "My favorite pets are cats."]
   result = {}
   for m in messages:
      resp = chat(model='llama3.1',
                  tools=tools,
                  messages=[{'role': 'system', 'content': prompt}] + examples + [{"role": "user", "content": m}],  # examples +
                  options={'temperatur': 0.0, "tools": "required", 'top_p': 0.1, "top_k": 20})
      try:
         mem = resp.message.tool_calls[0].function.arguments if len(resp.message.tool_calls) > 0 else {}
         for parcels_draft in mem['parcels_draft']:
            parcels_draft['last_seen'] = datetime.datetime.utcnow().isoformat(timespec="seconds")
         result[m] = mem
      except TypeError:
         parsed = parser.parse(resp.message.content)

         mem = parsed['parameters'] if 'parameters' in parsed else parsed
         for parcels_draft in mem['parcels_draft']:
            parcels_draft['last_seen'] = datetime.datetime.utcnow().isoformat(timespec="seconds")
      result[m] = resp.message.tool_calls[0].function.arguments if len(resp.message.tool_calls) > 0 else {}

   [print(f"Input:\n{k}\n\nOutput:\n{v}\n") for k, v in result.items()]
   schema = {
      "context": {
         "mode": None,
         "task": None,
         "step": None
      },
      "facts": [
         {
            "predicate": "",
            'entity': "",
            "value": "",
            "confidence": 0.5,
            "stability": 0.5,
            "last_seen": datetime.datetime.utcnow(),
            "evidence": "",
            'context': ""
         }
      ],
      "flags": {
         'awaiting_user_data': False,
         'high_confidence_update': False,
         'memory_conflict': False,
         'needs_clarification': False
      },
      "prefs": {
         'dislikes': [""],
         'likes': [""],
         'style': {}
      },
      "scratch": {
         'reasoning': None,
         'skipped_items': None}
   }
   return list(result.values())


def normalize_memory(payload):
   p = payload.copy()
   # 1) enforce full shapes
   p.setdefault("context", {"mode": None, "task": None, "step": None})
   p.setdefault("prefs", {"likes": [], "dislikes": [], "style": {}})
   p.setdefault("flags", {"awaiting_user_data": False, "needs_clarification": False,
                          "memory_conflict": False, "high_confidence_update": False})
   p.setdefault("scratch", {"reasoning": None, "skipped_items": None})

   # 2) drop spurious 'outside' style facts
   def keep(f):
      v = f.get("value", "").strip().lower()
      if f["predicate"] == "fact" and v in {"outside", "here", "now"}:
         return False
      return True

   p["facts"] = [f for f in p.get("facts", []) if keep(f)]

   # 3) hedge & stability nudges
   for f in p["facts"]:
      if f["predicate"] in {"like", "dislike"} and f["stability"] < 0.75:
         f["stability"] = 0.8
      if f["predicate"] in {"like", "dislike", "fact", "identity"} and f["confidence"] < 0.9 and "i think" not in f["evidence"].lower():
         f["confidence"] = max(f["confidence"], 0.9)
      # Proper noun casing for Python
      if f.get("value", "").lower() == "python":
         f["value"] = "Python"
      # Weather entity
      if f["predicate"] in {"fact", "like", "dislike"} and "snow" in f.get("value", "").lower():
         f["entity"] = "weather"

   # 4) high-confidence flag
   if any(f["confidence"] >= 0.95 and f["stability"] >= 0.8 for f in p["facts"]):
      p["flags"]["high_confidence_update"] = True

   # 5) clear unnecessary needs_clarification
   if p["facts"]:
      p["flags"]["needs_clarification"] = False

   return p


if __name__ == "__main__":
   memories = extract_facts([])
