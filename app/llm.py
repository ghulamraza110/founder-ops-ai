"""Shared LLM helper. Both providers expose OpenAI-compatible APIs.

Set LLM_PROVIDER in .env to "deepseek" (default) or "groq".
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
}


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    if provider not in PROVIDERS:
        raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    cfg = PROVIDERS[provider]
    api_key = os.getenv(cfg["key_env"])
    if not api_key:
        raise RuntimeError(f"{cfg['key_env']} is not set. Add it to your .env file.")
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", cfg["model"]),
        base_url=cfg["base_url"],
        api_key=api_key,
        temperature=temperature,
    )
