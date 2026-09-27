"""Canonical Pydantic data contracts — every hand-off is typed (Agent.MD §4).

Server-owned fields (ids, timestamps) use default factories so specialists never have to
emit them: fewer tokens, no hallucinated timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short_id() -> str:
    return uuid4().hex[:8]


# --- Plan ------------------------------------------------------------------

class SubQuestionStatus(str, Enum):
    pending = "pending"
    answered = "answered"
    failed = "failed"


class SubQuestion(BaseModel):
    id: str = Field(default_factory=_short_id)
    text: str
    rationale: str
    status: SubQuestionStatus = SubQuestionStatus.pending


class Plan(BaseModel):
    question: str
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


# --- Search & findings -----------------------------------------------------

class SearchStatus(str, Enum):
    ok = "ok"
    no_results = "no_results"
    rate_limited = "rate_limited"
    paywalled = "paywalled"
    timed_out = "timed_out"


class SourceDoc(BaseModel):
    """A fetched + extracted page the researcher reasons over (not a raw snippet).

    `source_id` is an internal immutable anchor minted at fetch time; findings/citations reference it
    rather than an attacker-influenced URL string (§4).
    """
    source_id: str = Field(default_factory=_short_id)
    url: str
    title: str = ""
    content: str
    content_sha256: str | None = None      # SHA-256 of the fetched bytes (provenance, §4)
    status_code: int | None = None
    content_type: str | None = None
    credibility_score: float = 0.0         # 0-1 heuristic source credibility
    fetched_at: datetime = Field(default_factory=_utcnow)


class SourceRef(BaseModel):
    """Immutable metadata of a successfully fetched source (NO page content) — the provenance anchor
    persisted in RunState. A finding is evidence only if its source_id matches one of these (§4)."""
    model_config = ConfigDict(extra="forbid")

    source_id: str
    url: str
    status_code: int | None = None
    content_type: str | None = None
    content_sha256: str | None = None
    credibility_score: float = 0.0
    fetched_at: datetime = Field(default_factory=_utcnow)

    @classmethod
    def from_doc(cls, doc: "SourceDoc") -> "SourceRef":
        return cls(
            source_id=doc.source_id, url=doc.url, status_code=doc.status_code,
            content_type=doc.content_type, content_sha256=doc.content_sha256,
            credibility_score=doc.credibility_score, fetched_at=doc.fetched_at,
        )


class SearchResult(BaseModel):
    """Structured result OR an explicit failure state — never a bare exception (§6)."""
    query: str
    status: SearchStatus = SearchStatus.ok
    docs: list[SourceDoc] = Field(default_factory=list)
    fetch_attempts: int = 0     # outbound fetch attempts made for this query (DoS budget accounting)


class Finding(BaseModel):
    """A finding without provenance is unusable — reject it (§4).

    `source_id` binds the finding to a real fetched SourceRef; it is set in code, never by the LLM.
    """
    id: str = Field(default_factory=_short_id)
    claim: str
    source_url: str
    source_id: str
    snippet: str
    sub_question_id: str
    credibility_score: float = 0.0         # inherited from the finding's source (§ credibility)
    retrieved_at: datetime = Field(default_factory=_utcnow)


# --- Report ----------------------------------------------------------------

class Contradiction(BaseModel):
    """Two findings from different sources making conflicting claims about one sub-question."""
    model_config = ConfigDict(extra="forbid")

    sub_question_id: str
    source_id_a: str
    source_id_b: str
    claim_a: str
    claim_b: str


class Citation(BaseModel):
    marker: str          # inline marker as it appears in the body, e.g. "[1]"
    finding_id: str      # references a real Finding.id (validated before shipping)
    source_url: str


class Report(BaseModel):
    question: str
    body: str
    citations: list[Citation] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utcnow)


# --- Trace & run state -----------------------------------------------------

class StepLog(BaseModel):
    step_id: str = Field(default_factory=_short_id)
    agent: str
    input: str
    output: str
    tools_called: list[str] = Field(default_factory=list)
    tokens: int = 0
    latency_ms: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)


class RunStatus(str, Enum):
    pending = "pending"
    planning = "planning"
    researching = "researching"
    writing = "writing"
    completed = "completed"
    failed = "failed"
    budget_exceeded = "budget_exceeded"          # generic (kept for back-compat)
    token_budget_exhausted = "token_budget_exhausted"
    search_budget_exhausted = "search_budget_exhausted"
    fetch_budget_exhausted = "fetch_budget_exhausted"
    wall_clock_exhausted = "wall_clock_exhausted"
    step_budget_exhausted = "step_budget_exhausted"
    cancelled = "cancelled"


# Statuses at which a run is finished and must never be re-executed.
TERMINAL_STATUSES = frozenset({
    RunStatus.completed, RunStatus.failed, RunStatus.budget_exceeded,
    RunStatus.token_budget_exhausted, RunStatus.search_budget_exhausted,
    RunStatus.fetch_budget_exhausted, RunStatus.wall_clock_exhausted,
    RunStatus.step_budget_exhausted, RunStatus.cancelled,
})

# Bump when RunState's persisted shape changes incompatibly; older state is rejected on load.
STATE_SCHEMA_VERSION = 3


class RunState(BaseModel):
    """Full persisted object — single source of truth, saved to Redis after every node (§3.4).

    Deserialized copies are validated (Pydantic + the invariant check below) before execution, so a
    corrupt or tampered blob fails safely instead of running (§6).
    """
    schema_version: int = STATE_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    question: str
    plan: Plan | None = None
    completed_sub_questions: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)  # provenance anchors (§4)
    contradictions: list[Contradiction] = Field(default_factory=list)  # cross-source conflicts
    cumulative_tokens: int = 0
    cumulative_cost: float = 0.0
    searches_used: int = 0                        # global budget counters (monotonic, never reset)
    fetches_used: int = 0
    status: RunStatus = RunStatus.pending
    cancel_requested: bool = False               # durable cancellation flag (§13)
    step_log: list[StepLog] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # --- final product ---
    report: Report | None = None

    # --- orchestration cursor (also what crash-resume restores in Phase 4, §8) ---
    current_index: int = 0                       # index into plan.sub_questions being researched
    send_backs_used: dict[str, int] = Field(default_factory=dict)  # sub_question_id -> count
    research_passes: dict[str, int] = Field(default_factory=dict)  # sub_question_id -> researcher passes done (idempotency)

    @model_validator(mode="after")
    def _check_invariants(self) -> "RunState":
        # Every accepted finding must reference a real fetched source (§4 provenance invariant).
        source_ids = {s.source_id for s in self.sources}
        for f in self.findings:
            if f.source_id not in source_ids:
                raise ValueError("finding references unknown source_id (provenance invariant)")
        return self
