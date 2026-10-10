"""Model access: Akamai's OpenAI-compatible workshop endpoint, driven through Strands."""
import os
from pathlib import Path

from dotenv import load_dotenv
from strands.models.openai import OpenAIModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def make_model(max_tokens: int = 1500, temperature: float = 0.2) -> OpenAIModel:
    """Always sets max_tokens: the endpoint defaults to 512 output tokens (maximum 4,096)."""
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not base_url or not api_key or api_key.startswith("paste_"):
        raise RuntimeError("Set OPENAI_BASE_URL and OPENAI_API_KEY in .env")
    return OpenAIModel(
        client_args={"base_url": base_url, "api_key": api_key, "timeout": 90.0, "max_retries": 2},
        model_id=os.environ.get("CHAT_MODEL", "hackathon-chat"),
        params={"max_tokens": max_tokens, "temperature": temperature},
    )
