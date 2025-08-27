import datetime

from ollama import chat


def extract_facts():
   with open('prompts/extract_facts.md', 'r') as fp:
      prompt = fp.read()
   now = datetime.datetime.utcnow().isoformat(timespec="seconds")
   tools = [{
      "type": "function",
      "function": {
         "name": "update_stm",
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
               "name": "update_stm",
               "arguments": {
                  "context": {"mode": "chess", "task": "teach openings", "step": 1},
                  "prefs": {"style": {}, "likes": [], "dislikes": []},
                  "facts": [
                     {"k": "topic", "v": "chess openings", "confidence": 0.9, "last_seen": now}
                  ],
                  "scratch": {},
                  "flags": {"needs_clarification": False, "awaiting_user_data": False}
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
            "id": "call_3",
            "type": "function",
            "function": {
               "name": "update_stm",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "prefs": {"style": {}, "likes": [], "dislikes": []},
                  "facts": [
                  ],
                  "scratch": {},
                  "flags": {"needs_clarification": False, "awaiting_user_data": False}
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
            "id": "call_4",
            "type": "function",
            "function": {
               "name": "update_stm",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "prefs": {"style": {}, "likes": [], "dislikes": []},
                  "facts": [
                     {"k": "weather", "v": "cold", "confidence": 0.95, "last_seen": now},
                  ],
                  "scratch": {},
                  "flags": {"needs_clarification": False, "awaiting_user_data": False}
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
            "id": "call_5",
            "type": "function",
            "function": {
               "name": "update_stm",
               "arguments": {
                  "context": {"mode": None, "task": None, "step": None},
                  "prefs": {"style": {}, "likes": ["chocolate ice cream"], "dislikes": []},
                  "facts": [
                     {"k": "food_like", "v": "chocolate ice cream", "confidence": 0.90, "last_seen": now},
                  ],
                  "scratch": {},
                  "flags": {"needs_clarification": False, "awaiting_user_data": False}
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
            "id": "call_6",
            "type": "function",
            "function": {
               "name": "update_stm",
               "arguments": {
                  "context": {"mode": 'chess', "task": 'play', "step": 1},
                  "prefs": {"style": {}, "likes": ["chess"], "dislikes": []},
                  "facts": [
                  ],
                  "scratch": {},
                  "flags": {"needs_clarification": False, "awaiting_user_data": True}
               }
            }
         }]
      }
   ]
   messages = ['I think pizza is delicous ', "It's raining outside", "My favorite pets are cats.", 'yes go ahead', "Do you want to play poker", "I understand", "I prefer python", "i just came from outside, and man i do hate snow!"]

   for m in messages:
      resp = chat(model='llama3.1',
                  tools=tools,
                  messages=[{'role': 'system', 'content': prompt}] + examples + [{"role": "user", "content": m}],
                  options={"tools": "required", 'temperatur': 0.0, 'top_p': 0.1, "top_k": 20})
      print(resp.message)


if __name__ == "__main__":
   extract_facts()
