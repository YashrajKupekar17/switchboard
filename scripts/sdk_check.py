"""Proves the gateway is a drop-in replacement: the official OpenAI SDK, only base_url changed."""
import os
import sys

from openai import OpenAI

base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/v1"
client = OpenAI(base_url=base_url, api_key=os.environ["SWITCHBOARD_API_KEY"])  # an sbk_ key from scripts/keys.py

resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Say hi in 3 words"}],
)
print("answer:", resp.choices[0].message.content)
print("usage: ", resp.usage.prompt_tokens, "in /", resp.usage.completion_tokens, "out")
