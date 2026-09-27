"""Tavily web search + real page fetch/extract → structured SearchResult (Agent.MD §6).

Discovery is Tavily; content is the *actual fetched page*, because search snippets are truncated
and misleading (§6). Every failure is converted into a SearchStatus, never a bare exception the
supervisor can't route around. Per-page content is capped to protect the token budget.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache

import httpx

from app.config import settings
from app.schemas import SearchResult, SearchStatus, SourceDoc

_FETCH_TIMEOUT = 10.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RAA-Researcher/1.0)"}
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


def _fetch_and_extract(url: str, title: str, client: httpx.Client) -> SourceDoc | None:
    """Fetch the real page and extract readable text. Per-URL failures return None (skip it)."""
    try:
        resp = client.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return None
    text = _html_to_text(resp.text)[: settings.max_page_chars]
    if not text:
        return None
    return SourceDoc(url=url, title=title, content=text)


def tavily_search(query: str) -> SearchResult:
    """Search + fetch/extract for one query. Returns a structured result or a failure state."""
    if not settings.tavily_api_key:
        return SearchResult(query=query, status=SearchStatus.no_results)

    try:
        raw = _client().search(
            query,
            max_results=settings.max_results_per_search,
            include_raw_content=True,
        )
    except Exception as exc:  # Tavily/transport failure → routable state (§6), never a crash
        return SearchResult(query=query, status=_classify(exc))

    hits = raw.get("results", []) if isinstance(raw, dict) else []
    if not hits:
        return SearchResult(query=query, status=SearchStatus.no_results)

    docs: list[SourceDoc] = []
    with httpx.Client() as client:
        for hit in hits:
            url = hit.get("url")
            if not url:
                continue
            title = hit.get("title") or ""
            doc = _fetch_and_extract(url, title, client)
            if doc is None:
                # Fall back to Tavily's server-side extraction (raw_content) — still a real page,
                # not the snippet. Skip the URL only if that is empty too.
                raw_content = (hit.get("raw_content") or "").strip()
                if raw_content:
                    doc = SourceDoc(url=url, title=title, content=raw_content[: settings.max_page_chars])
            if doc is not None:
                docs.append(doc)

    if not docs:
        return SearchResult(query=query, status=SearchStatus.no_results)
    return SearchResult(query=query, status=SearchStatus.ok, docs=docs)


def get_search_fn():
    """Live search callable when a Tavily key is configured, else None so the graph degrades to
    zero findings (a valid empty report) instead of failing."""
    return tavily_search if settings.tavily_api_key else None
