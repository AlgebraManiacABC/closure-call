"""Checks the Akamai workshop endpoint: which model backs it, and whether Strands can drive it.

Run from the repo root:  python scripts/check_endpoint.py
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from strands import Agent, tool
from strands.models.openai import OpenAIModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "hackathon-chat")
if not BASE_URL or not API_KEY or API_KEY.startswith("paste_"):
    sys.exit("Fill in OPENAI_BASE_URL and OPENAI_API_KEY in .env first.")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=60.0)

print("== 1. Models the endpoint lists ==")
for model in client.models.list().data:
    print(" ", model.model_dump())

print("\n== 2. Plain chat call ==")
reply = client.chat.completions.create(
    model=CHAT_MODEL,
    messages=[{"role": "user", "content": "Reply with the single word: ready"}],
    max_tokens=20,
)
print("  backing model:", reply.model)
print("  answer:", reply.choices[0].message.content)

print("\n== 3. Strands agent with one tool ==")
calls = []


@tool
def river_stage(gauge_id: str) -> str:
    """Get the current river stage in feet for a USGS gauge.

    Args:
        gauge_id: The USGS gauge id, for example 06893000.
    """
    calls.append(gauge_id)
    return f"Gauge {gauge_id}: stage 24.7 ft, flood stage 23.0 ft."


agent = Agent(
    model=OpenAIModel(
        client_args={"base_url": BASE_URL, "api_key": API_KEY},
        model_id=CHAT_MODEL,
        params={"max_tokens": 1024, "temperature": 0.3},
    ),
    tools=[river_stage],
    system_prompt="Use your tools to answer. Be brief.",
    callback_handler=None,
)
result = agent("Is gauge 06893000 above flood stage, and by how much?")
print("  tool called with:", calls or "NOTHING (tool was not used)")
print("  answer:", str(result).strip())
print("\nRESULT:", "PASS, Strands works with this endpoint" if calls else "FAIL, the model answered without the tool")
