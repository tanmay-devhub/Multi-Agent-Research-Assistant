"""Tavily discovery + SSRF-safe page fetch → structured SearchResult (Agent hardening §1, §4, §6).

Tavily is used ONLY to discover candidate URLs. Content always comes from our own SSRF-safe fetch
(`app.security.secure_fetch`); Tavily snippets / raw_content are never treated as evidence. A page
becomes a SourceDoc only if our fetch succeeds. Every failure is a SearchStatus, never an exception.
"""
from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from app.config import settings
from app.credibility import score_source
from app.schemas import SearchResult, SearchStatus, SourceDoc
from app.security import normalize_url, secure_fetch

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@lru_cache(maxsize=1)
def _client():
    from tavily import TavilyClient

    return TavilyClient(api_key=settings.tavily_api_key)


def _html_to_text(raw: str) -> str:
    raw = _SCRIPT_STYLE.sub(" ", raw)
    raw = _TAG.sub(" ", raw)
    return _WS.sub(" ", html.unescape(raw)).strip()


def _classify(exc: Exception) -> SearchStatus:
    """Best-effort mapping of a Tavily/transport error to a routable failure state."""
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "quota" in msg:
        return SearchStatus.rate_limited
    if "402" in msg or "403" in msg or "401" in msg or "paywall" in msg:
        return SearchStatus.paywalled
    return SearchStatus.timed_out


def tavily_search(query: str) -> SearchResult:
    """Search + SSRF-safe fetch/extract for one query. Structured result or a failure state."""
    if not settings.tavily_api_key:
        return SearchResult(query=query, status=SearchStatus.no_results)

    try:
        raw = _client().search(query, max_results=settings.max_results_per_search)
    except Exception as exc:  # Tavily/transport failure → routable state (§6), never a crash
        return SearchResult(query=query, status=_classify(exc))

    hits = raw.get("results", []) if isinstance(raw, dict) else []
    if not hits:
        return SearchResult(query=query, status=SearchStatus.no_results)

    # De-dup candidate URLs (canonical form), preserving Tavily's ranking order.
    candidates: list[tuple[str, str]] = []   # (canonical_url, title)
    seen: set[str] = set()
    for hit in hits:
        url = normalize_url(hit.get("url") or "")
        if url is None or url in seen:
            continue
        seen.add(url)
        candidates.append((url, (hit.get("title") or "")[: settings.max_title_chars]))

    attempts = len(candidates)
    # Fetch in parallel (bounded) — same sources, far less wall-time. secure_fetch is self-contained
    # per call (own client/DNS), so it is thread-safe; it never raises.
    docs: list[SourceDoc] = []
    if candidates:
        workers = max(1, min(settings.max_concurrent_fetches, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(lambda c: (c, secure_fetch(c[0])), candidates))
        for (url, title), outcome in outcomes:   # order preserved by pool.map
            if not outcome.ok:
                continue                     # blocked/oversized/failed URLs are simply dropped
            text = _html_to_text(outcome.text)[: settings.max_page_chars]
            if not text:
                continue
            final_url = outcome.final_url or url
            docs.append(SourceDoc(
                url=final_url,
                title=title,
                content=text,
                content_sha256=outcome.content_sha256,
                status_code=outcome.status_code,
                content_type=(outcome.content_type or "")[:100] or None,
                credibility_score=score_source(
                    final_url, outcome.status_code, outcome.elapsed_ms,
                    outcome.redirects, text, title,
                ),
            ))

    if not docs:
        return SearchResult(query=query, status=SearchStatus.no_results, fetch_attempts=attempts)
    return SearchResult(query=query, status=SearchStatus.ok, docs=docs, fetch_attempts=attempts)


def get_search_fn():
    """Live search callable when a Tavily key is configured, else None so the graph degrades to
    zero findings (a valid empty report) instead of failing."""
    return tavily_search if settings.tavily_api_key else None
