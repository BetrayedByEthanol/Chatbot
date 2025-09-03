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
         "name": "update_memory",
         "description": "Return STM patch.",
         "parameters": {
            "type": "object",
            "properties": {
               "context": {"type": "object", "properties": {
                  "mode": {"type": ["string", "null"]}, "task": {"type": ["string", "null"]}, "step": {"type": ["integer", "null"]}}},
               "prefs": {"type": "object", "properties": {
                  "style": {"type": "object"}, "likes": {"type": "array", "items": {"type": "string"}}, "dislikes": {"type": "array", "items": {"type": "string"}}}},
               "facts": {"type": "array", "items": {"type": "object", "properties": {
                  "k": {"type": "string"}, "v": {"type": "string"}, "confidence": {"type": "number"}, "last_seen": {"type": "string"}}, "required": ["k", "v", "confidence", "last_seen"]}},
               "scratch": {"type": "object"},
               "flags": {"type": "object", "properties": {
                  "needs_clarification": {"type": "boolean"}, "awaiting_user_data": {"type": "boolean"}}}
            },
            "required": ["context", "prefs", "facts", "scratch", "flags"],
            "additionalProperties": False
         }
      }}]

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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": "teach", "step": 1},
                  "facts": [
                     {
                        "predicate": "goal",
                        "entity": "skill",
                        "value": "chess",
                        "confidence": 0.70,
                        "stability": 0.30,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "Can you teach me some opening chess moves?"
                     }
                  ],
                  "prefs": {"likes": ["chess"], "dislikes": [], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "facts": [],
                  "prefs": {"likes": [], "dislikes": [], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "facts": [
                     {
                        "predicate": "fact",
                        "entity": "weather",
                        "value": "cold",
                        "confidence": 0.95,
                        "stability": 0.25,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "cold outside"
                     }
                  ],
                  "prefs": {"likes": [], "dislikes": [], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "facts": [
                     {
                        "predicate": "like",
                        "entity": "food",
                        "value": "chocolate ice cream",
                        "confidence": 0.75,
                        "stability": 0.85,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "Chocolate ice cream is yummy"
                     }
                  ],
                  "prefs": {"likes": ["chocolate ice cream"], "dislikes": [], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": 'play chess', "step": 1},
                  "facts": [
                     {
                        "predicate": "goal",
                        "entity": "skill",
                        "value": "chess",
                        "confidence": 0.70,
                        "stability": 0.30,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "Can you teach me some opening chess moves?"
                     }
                  ],
                  "prefs": {"likes": ["chess"], "dislikes": [], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
               "name": "update_memory",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "facts": [
                     {
                        "predicate": "fact",
                        "entity": "weather",
                        "value": "rain",
                        "confidence": 0.70,
                        "stability": 0.30,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "It's raining outside"
                     }, {
                        "predicate": "dislike",
                        "entity": "weather",
                        "value": "rain",
                        "confidence": 0.70,
                        "stability": 0.60,
                        "last_seen": "2025-08-28T10:32:00Z",
                        "evidence": "I do not like the rain"
                     }
                  ],
                  "prefs": {"likes": [], "dislikes": ["rain"], "style": {}},
                  "flags": {"awaiting_user_data": False, "needs_clarification": False},
                  "scratch": {}
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
                  messages=[{'role': 'system', 'content': prompt}] + [{"role": "user", "content": m}],  # examples +
                  options={"tools": "required", 'temperatur': 0.0, 'top_p': 0.1, "top_k": 20})
      try:
         mem = resp.message.tool_calls[0].function.arguments if len(resp.message.tool_calls) > 0 else {}
         for fact in mem['facts']:
            fact['last_seen'] = datetime.datetime.utcnow().isoformat(timespec="seconds")
         result[m] = mem
      except TypeError:
         parsed = parser.parse(resp.message.content)

         mem = parsed['parameters'] if 'parameters' in parsed else parsed
         for fact in mem['facts']:
            fact['last_seen'] = datetime.datetime.utcnow().isoformat(timespec="seconds")
         result[m] = mem

   [print(x) for x in result.values()]
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
