"""LLM provider chain — a primary model with automatic fallbacks (Agent.MD §2).

Providers are cloud-only. Gemini uses its native SDK; Ollama Cloud and OpenRouter are both
OpenAI-compatible, so they go through `ChatOpenAI` (which sends the `Authorization: Bearer`
token cleanly — `langchain-ollama` cannot reliably pass that header). The provider order comes
from `settings.provider_chain()`; on error LangChain falls through to the next provider.

Callers use `structured(Schema)` so every hand-off stays a validated Pydantic object (§10.2).
"""
from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler

from app.config import settings


class TokenMeter(BaseCallbackHandler):
    """Accumulates total token usage across LLM calls via provider usage metadata.

    Attach with `config=meter.config`, then read `meter.total_tokens`. Counting depends on the
    provider populating `usage_metadata` (Gemini and OpenAI-compatible endpoints do); if a
    provider omits it, tokens stay 0 and the wall-clock budget still guards the run.
    """

    def __init__(self) -> None:
        self.total_tokens = 0

    def on_llm_end(self, response, **kwargs) -> None:
        for generations in getattr(response, "generations", []) or []:
            for gen in generations:
                message = getattr(gen, "message", None)
                usage = getattr(message, "usage_metadata", None) if message is not None else None
                if usage:
                    self.total_tokens += int(usage.get("total_tokens", 0) or 0)

    @property
    def config(self) -> dict:
        return {"callbacks": [self]}


def _make_model(provider: str, temperature: float):
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=temperature,
            google_api_key=settings.google_api_key,
            max_retries=0,  # fail fast → let the fallback chain engage instead of backing off
        )

    from langchain_openai import ChatOpenAI  # all remaining providers are OpenAI-compatible

    if provider == "ollama_cloud":
        return ChatOpenAI(
            model=settings.ollama_model,
            temperature=temperature,
            api_key=settings.ollama_api_key,
            base_url=settings.ollama_base_url,
            max_retries=0,  # fail fast → let the fallback chain engage instead of backing off
        )
    if provider == "openrouter":
        return ChatOpenAI(
            model=settings.openrouter_model,
            temperature=temperature,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            max_retries=0,
        )
    if provider == "openai_compatible":
        return ChatOpenAI(
            model=settings.openai_model,
            temperature=temperature,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            max_retries=0,
        )
    raise ValueError(f"Unknown provider: {provider!r}")


def _temp(temperature: float | None) -> float:
    return settings.llm_temperature if temperature is None else temperature


def get_chat_model(temperature: float | None = None):
    """Primary chat model with ordered fallbacks (plain, non-structured calls)."""
    temp = _temp(temperature)
    chain = settings.provider_chain()
    primary = _make_model(chain[0], temp)
    fallbacks = [_make_model(p, temp) for p in chain[1:]]
    return primary.with_fallbacks(fallbacks) if fallbacks else primary


def structured(schema, temperature: float | None = None):
    """Primary structured-output model with ordered fallbacks; returns a validated `schema`."""
    temp = _temp(temperature)
    chain = settings.provider_chain()
    primary = _make_model(chain[0], temp).with_structured_output(schema)
    fallbacks = [_make_model(p, temp).with_structured_output(schema) for p in chain[1:]]
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
