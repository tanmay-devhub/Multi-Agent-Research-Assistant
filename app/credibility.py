"""Heuristic source credibility scoring (0.0–1.0).

Deterministic, code-only — no LLM. Signals (additive):
  HTTPS 0.1 · domain tier (.gov/.edu 0.3, major press/journals 0.2, else 0.1) · fast (<2s) 0.1 ·
  content >500 chars 0.1 · HTTP 200 0.1 · ≤2 redirect hops 0.1 · has title 0.1.
Max ~0.9; clamped to [0,1]. Used to rank findings and flag low-credibility sources in the report.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# Curated, conservative allowlist of high-trust press + journals (0.2 tier).
_MAJOR_PRESS = frozenset({
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "washingtonpost.com",
    "theguardian.com", "wsj.com", "bloomberg.com", "npr.org", "economist.com", "ft.com",
    "nature.com", "science.org", "arxiv.org", "acm.org", "ieee.org", "springer.com",
})


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except (ValueError, UnicodeError):
        return ""


def _domain_tier(host: str) -> float:
    if host.endswith(".gov") or host.endswith(".edu") or ".gov." in host or ".ac." in host or ".edu." in host:
        return 0.3
    if host in _MAJOR_PRESS or any(host.endswith("." + d) for d in _MAJOR_PRESS):
        return 0.2
    return 0.1


def score_source(
    url: str,
    status_code: int | None,
    elapsed_ms: int,
    redirects: int,
    text: str,
    title: str,
) -> float:
    s = 0.0
    if url.lower().startswith("https://"):
        s += 0.1
    s += _domain_tier(_host(url))
    if 0 < elapsed_ms < 2000:
        s += 0.1
    if len(text) > 500:
        s += 0.1
    if status_code == 200:
        s += 0.1
    if redirects <= 2:
        s += 0.1
    if title.strip():
        s += 0.1
    return round(min(1.0, max(0.0, s)), 3)
