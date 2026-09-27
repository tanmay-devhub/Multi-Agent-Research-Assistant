"""Writer specialist: Plan + findings -> Report with validated citations (Agent.MD §3.2, §7).

The model writes the body with inline [n] markers and a citations list keyed by finding id.
Citations that don't map to a real finding are dropped in code (a citation must reference a real
finding record). Full body-vs-citation validation lands in Phase 5.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm import structured
from app.schemas import Citation, Finding, Plan, Report


class _CitationDraft(BaseModel):
    marker: str = Field(description="Inline citation marker used in the body, e.g. '[1]'.")
    finding_id: str = Field(description="ID of the finding that supports the cited claim.")


class _WriterOutput(BaseModel):
    body: str = Field(description="Report body; every factual claim carries an inline marker like [1].")
    citations: list[_CitationDraft]


_SYSTEM = (
    "Write a concise, well-structured report that answers the question using ONLY the provided "
    "findings. Every factual claim must carry an inline citation marker (e.g. [1]) that maps to a "
    "finding id in the citations list. Do not use any fact that is absent from the findings."
)


def write(plan: Plan, findings: list[Finding], config: dict | None = None) -> Report:
    model = structured(_WriterOutput)
    findings_blob = "\n".join(
        f"[{f.id}] {f.claim} (source: {f.source_url})" for f in findings
    )
    out: _WriterOutput = model.invoke(
        [
            ("system", _SYSTEM),
            ("human", f"QUESTION: {plan.question}\n\nFINDINGS:\n{findings_blob}"),
        ],
        config=config,
    )

    by_id = {f.id: f for f in findings}
    citations = [
        Citation(marker=c.marker, finding_id=c.finding_id, source_url=by_id[c.finding_id].source_url)
        for c in out.citations
        if c.finding_id in by_id
    ]
    return Report(question=plan.question, body=out.body, citations=citations)
