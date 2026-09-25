"""Which chat model the agents run on.

- LLM_PROVIDER=stub (default): deterministic rule-based model, no key, used by
  tests, CI and the offline demo.
- LLM_PROVIDER=groq: real hosted model via langchain-groq. Needs GROQ_API_KEY
  (free at https://console.groq.com). Also unlocks voice orders via Groq's
  Whisper endpoint. The platform still answers every message it can with the
  deterministic language layer first; the model only sees what that layer
  didn't understand (see platform.MunshiPlatform).
- LLM_PROVIDER=gemini: Google Gemini via langchain-google-genai. Needs GOOGLE_API_KEY
  (Google AI Studio). Default model gemini-flash-latest; gemini-3.5-flash-lite is faster.
  Same hybrid: the model only sees what the deterministic layer didn't understand.

Request limits (env, all optional):
  LLM_TIMEOUT_S         per-request timeout in seconds (default 30)
  LLM_MAX_RETRIES       retries on timeouts, 429s and 5xx, with the SDK's backoff (default 2)
  LLM_REASONING_EFFORT  low | medium | high for reasoning models (default low for openai/gpt-oss-*;
                        'none' leaves the provider default)
"""
from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


def _float_env(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v >= 0 else default
    except ValueError:
        return default


def request_limits() -> dict:
    """The timeout / retry settings a real model is built with (read from the environment)."""
    return {"timeout_s": _float_env("LLM_TIMEOUT_S", 30.0), "max_retries": _int_env("LLM_MAX_RETRIES", 2)}


def build_chat_model(provider: str | None = None) -> BaseChatModel | None:
    provider = (provider or os.environ.get("LLM_PROVIDER", "stub")).lower()
    if provider == "stub":
        return None
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise ValueError("LLM_PROVIDER=groq needs GROQ_API_KEY (free key: https://console.groq.com). See .env.example.")
        from langchain_groq import ChatGroq
        model = os.environ.get("LLM_MODEL", DEFAULT_GROQ_MODEL)
        lim = request_limits()
        effort = os.environ.get("LLM_REASONING_EFFORT", "low" if model.startswith("openai/gpt-oss") else "none").strip().lower()
        extra = {"reasoning_effort": effort} if effort and effort != "none" else {}
        return ChatGroq(model=model, api_key=key, temperature=0, request_timeout=lim["timeout_s"], max_retries=lim["max_retries"], **extra)
    if provider == "gemini":
        key = os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError("LLM_PROVIDER=gemini needs GOOGLE_API_KEY (Google AI Studio: https://aistudio.google.com/apikey). See .env.example.")
        from langchain_google_genai import ChatGoogleGenerativeAI
        lim = request_limits()
        return ChatGoogleGenerativeAI(model=os.environ.get("LLM_MODEL", DEFAULT_GEMINI_MODEL), google_api_key=key, temperature=0,
                                      timeout=lim["timeout_s"], max_retries=lim["max_retries"])
    raise ValueError(f"unknown LLM_PROVIDER {provider!r}")
