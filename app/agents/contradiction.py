"""Cross-source contradiction detection (post-research analysis).

Given the run's findings, an LLM flags pairs of findings that make CONFLICTING claims about the same
sub-question. Deterministic guards, never trusting the model: only real finding ids are accepted,
the two findings must belong to the same sub-question AND different sources, and the count is capped.
The authoritative claims/source_ids come from our Finding records, not from LLM output.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import settings
from app.llm import structured
from app.schemas import Contradiction, Finding

_SYSTEM = (
    "You are given research findings, each with an id and the sub-question it answers. Identify "
    "pairs of findings that DIRECTLY CONTRADICT each other (make claims that cannot both be true) "
    "about the SAME sub-question. Return only genuine conflicts as pairs of finding ids. If there "
    "are no real contradictions, return an empty list. Use only the provided finding ids."
)


class _Pair(BaseModel):
    finding_id_a: str = Field(description="id of the first finding in the conflicting pair")
    finding_id_b: str = Field(description="id of the second finding in the conflicting pair")


class _Conflicts(BaseModel):
    pairs: list[_Pair] = Field(default_factory=list)


def detect(findings: list[Finding], config: dict | None = None) -> list[Contradiction]:
    # Only sub-questions with >=2 findings spanning >=2 distinct sources can contain a contradiction.
    by_sub: dict[str, list[Finding]] = {}
    for f in findings:
        by_sub.setdefault(f.sub_question_id, []).append(f)
    eligible = [
        f for group in by_sub.values() if len({g.source_id for g in group}) >= 2 for f in group
    ]
    if len(eligible) < 2:
        return []

    by_id = {f.id: f for f in eligible}
    blob = "\n".join(
        f"[{f.id}] (sub_question={f.sub_question_id}) {f.claim}" for f in eligible
    )
    out: _Conflicts = structured(_Conflicts).invoke(
        [("system", _SYSTEM), ("human", f"FINDINGS:\n{blob}")], config=config
    )

    seen: set[tuple[str, str]] = set()
    contradictions: list[Contradiction] = []
    for pair in out.pairs:
        a, b = by_id.get(pair.finding_id_a), by_id.get(pair.finding_id_b)
        if a is None or b is None or a.id == b.id:
            continue                                   # reject invented/self ids
        if a.sub_question_id != b.sub_question_id:
            continue                                   # must concern the same sub-question
        if a.source_id == b.source_id:
            continue                                   # must be different sources
        key = tuple(sorted((a.id, b.id)))
        if key in seen:
            continue
        seen.add(key)
        contradictions.append(Contradiction(
            sub_question_id=a.sub_question_id,
            source_id_a=a.source_id, source_id_b=b.source_id,
            claim_a=a.claim, claim_b=b.claim,
        ))
        if len(contradictions) >= settings.max_findings_total:
            break
    return contradictions
