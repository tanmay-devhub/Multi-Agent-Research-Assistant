"""Centralized secret redaction (Agent hardening §8, §11).

Applied before anything reaches a trace, log, API response, or provider error. Redacts both the
concrete secret values this process holds AND anything matching known secret shapes, so a key that
leaks via an unexpected path is still scrubbed.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Header/field names whose values are always secret-bearing.
_SECRET_KEYS = frozenset(
    {
        "authorization", "auth", "proxy-authorization", "cookie", "set-cookie",
        "x-api-key", "api-key", "apikey", "api_key", "token", "access_token",
        "refresh_token", "password", "passwd", "secret", "client_secret",
        "bearer", "x-goog-api-key", "google_api_key", "openrouter_api_key",
        "ollama_api_key", "tavily_api_key", "openai_api_key", "redis_url",
    }
)

# Value shapes that are secrets regardless of surrounding key.
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),                 # OpenAI / OpenRouter style
    re.compile(r"sk-or-[A-Za-z0-9_\-]{8,}"),              # OpenRouter
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),               # Google API key
    re.compile(r"AQ\.[A-Za-z0-9_\-]{16,}"),               # Google short-lived key form
    re.compile(r"tvly-[A-Za-z0-9_\-]{8,}"),               # Tavily
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),         # bearer tokens
    re.compile(r"(?i)rediss?://[^\s\"'<>]+"),             # redis URL (may carry credentials)
]


def _known_secret_values() -> list[str]:
    """Concrete secrets held by this process, longest-first so substrings don't leak."""
    from app.config import settings  # lazy: avoid import cycle at module load

    names = (
        "google_api_key", "openrouter_api_key", "ollama_api_key",
        "tavily_api_key", "openai_api_key", "redis_url",
    )
    vals: list[str] = []
    for name in names:
        v = getattr(settings, name, None)
        if isinstance(v, str) and len(v) >= 6:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: Any) -> str:
    """Scrub secrets from an arbitrary string (or stringified value)."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    for v in _known_secret_values():
        if v:
            s = s.replace(v, REDACTED)
    for pat in _PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


def redact_mapping(data: dict) -> dict:
    """Redact a header/field mapping: secret-named keys are fully masked, others value-scrubbed."""
    out: dict = {}
    for k, v in data.items():
        if str(k).lower() in _SECRET_KEYS:
            out[k] = REDACTED
        elif isinstance(v, dict):
            out[k] = redact_mapping(v)
        elif isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = v
    return out
