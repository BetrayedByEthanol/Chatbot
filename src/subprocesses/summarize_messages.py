def summarize_message(messages: list):
   if len(messages) / 3 > 400:
      return "To long"
   return ''.join(messages)
