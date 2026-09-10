"""Which chat model the agents run on.

- LLM_PROVIDER=stub (default): deterministic rule-based model, no key, used by
  tests, CI and the offline demo.
- LLM_PROVIDER=groq: real hosted model via langchain-groq. Needs GROQ_API_KEY
  (free at https://console.groq.com). Also unlocks voice orders via Groq's
  Whisper endpoint.
"""
from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel


def build_chat_model(provider: str | None = None) -> BaseChatModel | None:
    provider = (provider or os.environ.get("LLM_PROVIDER", "stub")).lower()
    if provider == "stub":
        return None
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise ValueError("LLM_PROVIDER=groq needs GROQ_API_KEY (free key: https://console.groq.com). See .env.example.")
        from langchain_groq import ChatGroq
        return ChatGroq(model=os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"), api_key=key, temperature=0)
    raise ValueError(f"unknown LLM_PROVIDER {provider!r}")
