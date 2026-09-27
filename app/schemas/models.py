"""Canonical Pydantic data contracts — every hand-off is typed (Agent.MD §4).

Server-owned fields (ids, timestamps) use default factories so specialists never have to
emit them: fewer tokens, no hallucinated timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


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
    """A fetched + extracted page the researcher reasons over (not a raw snippet)."""
    url: str
    title: str = ""
    content: str
    fetched_at: datetime = Field(default_factory=_utcnow)


class SearchResult(BaseModel):
    """Structured result OR an explicit failure state — never a bare exception (§6)."""
    query: str
    status: SearchStatus = SearchStatus.ok
    docs: list[SourceDoc] = Field(default_factory=list)


class Finding(BaseModel):
    """A finding without provenance is unusable — reject it (§4)."""
    id: str = Field(default_factory=_short_id)
    claim: str
    source_url: str
    snippet: str
    sub_question_id: str
    retrieved_at: datetime = Field(default_factory=_utcnow)


# --- Report ----------------------------------------------------------------

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
    budget_exceeded = "budget_exceeded"


class RunState(BaseModel):
    """Full persisted object — single source of truth, saved to Redis after every node (§3.4)."""
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    question: str
    plan: Plan | None = None
    completed_sub_questions: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    cumulative_tokens: int = 0
    cumulative_cost: float = 0.0
    status: RunStatus = RunStatus.pending
    step_log: list[StepLog] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # --- final product ---
    report: Report | None = None

    # --- orchestration cursor (also what crash-resume restores in Phase 4, §8) ---
    current_index: int = 0                       # index into plan.sub_questions being researched
    send_backs_used: dict[str, int] = Field(default_factory=dict)  # sub_question_id -> count
    research_passes: dict[str, int] = Field(default_factory=dict)  # sub_question_id -> researcher passes done (idempotency)
