import datetime
import pathlib
from ollama import chat


def extract_facts(messages: list):
   pth = pathlib.Path(__file__).parent
   with open(str(pth) + '/prompts/extract_facts.md', 'r') as fp:
      prompt = fp.read()
   now = datetime.datetime.utcnow().isoformat(timespec="seconds")
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
   result = {}
   for m in messages:
      resp = chat(model='llama3.1',
                  tools=tools,
                  messages=[{'role': 'system', 'content': prompt}] + [{"role": "user", "content": m}], #  examples +
                  options={"tools": "required", 'temperatur': 0.0, 'top_p': 0.1, "top_k": 20})
      try:
         result[m] = resp.message.tool_calls[0].function.arguments if len(resp.message.tool_calls) > 0 else {}
         print(resp.message.tool_calls[0].function.arguments if len(resp.message.tool_calls) > 0 else {})
      except TypeError:
         print(resp.message.content)
   return result


if __name__ == "__main__":
   extract_facts([])
