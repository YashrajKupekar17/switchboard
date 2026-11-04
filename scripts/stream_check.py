"""Streams a real answer through the gateway with the official OpenAI SDK and prints tokens as they arrive."""
import sys
import time

from openai import OpenAI

base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/v1"
client = OpenAI(base_url=base_url, api_key="not-checked-yet")

start = time.perf_counter()
first = None
stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Count from 1 to 15, separated by spaces."}],
    stream=True,
)
for chunk in stream:
    for choice in chunk.choices:
        if choice.delta.content:
            first = first or time.perf_counter()
            print(choice.delta.content, end="", flush=True)
print(f"\n\nclient-side TTFT: {first - start:.3f}s   total: {time.perf_counter() - start:.3f}s")
