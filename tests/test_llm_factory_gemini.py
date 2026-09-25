"""LLM_PROVIDER=gemini builds a Gemini chat model from GOOGLE_API_KEY (no network call at build time)."""
from __future__ import annotations

import pytest

from munshi.llm.factory import DEFAULT_GEMINI_MODEL, build_chat_model


def test_gemini_needs_a_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
        build_chat_model("gemini")


def test_gemini_model_uses_env_model_and_request_limits(monkeypatch):
    pytest.importorskip("langchain_google_genai")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_TIMEOUT_S", "12")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    m = build_chat_model("gemini")
    assert type(m).__name__ == "ChatGoogleGenerativeAI"
    assert m.model.endswith(DEFAULT_GEMINI_MODEL) and m.temperature == 0
    assert m.timeout == 12 and m.max_retries == 1
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash-lite")
    assert build_chat_model("gemini").model.endswith("gemini-3.5-flash-lite")
