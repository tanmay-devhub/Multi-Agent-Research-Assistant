"""Security primitives: secret redaction, URL normalization, and SSRF-safe web fetch.

Centralized so every layer (agents, tools, API, trace) shares one enforcement point. Nothing here
trusts external input — user text, LLM output, URLs, DNS, redirects, and fetched pages are all
treated as hostile (see SECURITY.md).
"""
from app.security.fetch import FetchOutcome, secure_fetch
from app.security.redaction import redact, redact_mapping
from app.security.urls import normalize_url, url_host

__all__ = [
    "FetchOutcome",
    "secure_fetch",
    "redact",
    "redact_mapping",
    "normalize_url",
    "url_host",
]
