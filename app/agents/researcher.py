"""Researcher specialist: one SubQuestion -> list[Finding] with provenance (Agent.MD §3.2).

Two model calls: (1) generate bounded search queries, (2) synthesize findings from the fetched
sources. Search is dependency-injected via `search_fn` because live Tavily search arrives in
Phase 3; provenance is enforced in code (findings whose URL isn't in the sources are dropped).
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.config import settings
from app.llm import structured
from app.schemas import Finding, SearchResult, SearchStatus, SourceDoc, SubQuestion

SearchFn = Callable[[str], SearchResult]


class _QueryPlan(BaseModel):
    queries: list[str] = Field(description="Focused, varied web-search queries for the sub-question.")


class _FindingDraft(BaseModel):
    claim: str = Field(description="A single factual claim that answers part of the sub-question.")
    source_url: str = Field(description="URL of the provided source that supports the claim.")
    snippet: str = Field(description="Short verbatim excerpt from that source supporting the claim.")


class _Synthesis(BaseModel):
    findings: list[_FindingDraft]


_QUERY_SYSTEM = (
    "Produce focused web-search queries for the given sub-question "
    f"(at most {settings.max_searches_per_sub_question}). Make them specific and varied."
)
_SYNTH_SYSTEM = (
    "From the provided sources only, extract findings that answer the sub-question. Every finding "
    "must cite the exact source URL it came from and include a short verbatim snippet. Do not "
    "invent sources, URLs, or facts that are not present in the provided content."
)


def _default_search(query: str) -> SearchResult:
    raise NotImplementedError(
        "Live search arrives in Phase 3 — pass a search_fn to research() until then."
    )


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def research(
    sub_question: SubQuestion,
    search_fn: SearchFn = _default_search,
    config: dict | None = None,
) -> list[Finding]:
    query_plan: _QueryPlan = structured(_QueryPlan).invoke(
        [("system", _QUERY_SYSTEM), ("human", sub_question.text)], config=config
    )
    queries = query_plan.queries[: settings.max_searches_per_sub_question]

    docs: list[SourceDoc] = []
    seen_urls: set[str] = set()
    domain_counts: dict[str, int] = {}
    for query in queries:
        result = search_fn(query)
        if result.status is not SearchStatus.ok:
            continue  # failure states are routed around; here we simply skip
        for doc in result.docs:
            if doc.url in seen_urls:
                continue  # dedup by URL (§5)
            domain = _domain(doc.url)
            if domain_counts.get(domain, 0) >= settings.per_domain_cap:
                continue  # one site cannot dominate the report (§5)
            seen_urls.add(doc.url)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            docs.append(doc)

    if not docs:
        return []

    sources_blob = "\n\n".join(
        f"URL: {doc.url}\nTITLE: {doc.title}\n{doc.content}" for doc in docs
    )
    synthesis: _Synthesis = structured(_Synthesis).invoke(
        [
            ("system", _SYNTH_SYSTEM),
            ("human", f"SUB-QUESTION: {sub_question.text}\n\nSOURCES:\n{sources_blob}"),
        ],
        config=config,
    )

    valid_urls = seen_urls
    return [
        Finding(
            claim=draft.claim,
            source_url=draft.source_url,
            snippet=draft.snippet,
            sub_question_id=sub_question.id,
        )
        for draft in synthesis.findings
        if draft.source_url in valid_urls
    ]
