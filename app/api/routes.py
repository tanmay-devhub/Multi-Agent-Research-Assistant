"""FastAPI routes (Agent.MD §9, hardening §9/§10).

The graph persists full state to Redis after every node (§8), so the API never drives the graph
inline: POST starts a background run, and status / trace / stream are views over persisted state.
Every run-scoped endpoint passes through `require_run_access` — knowing a run_id is not authorization.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import (
    Principal,
    enforce_quota,
    get_principal,
    rate_limit,
    require_run_access,
)
from app.config import settings
from app.graph.builder import run_research
from app.schemas import TERMINAL_STATUSES, RunState, RunStatus
from app.store import redis_store

router = APIRouter()

_TERMINAL = TERMINAL_STATUSES
_INDEX_HTML = Path(__file__).parent / "index.html"


# --- strict request/response schemas (§3, §9) --------------------------------

class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=settings.max_question_chars)


class ResearchAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: RunStatus


class CitationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marker: str
    finding_id: str
    source_url: str


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str
    citations: list[CitationOut]
    citations_valid: bool


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    question: str
    status: RunStatus
    num_sub_questions: int
    num_findings: int
    num_sources: int
    cumulative_tokens: int
    searches_used: int
    fetches_used: int
    report: ReportOut | None = None


# --- run lifecycle -----------------------------------------------------------

def _run_in_background(question: str, run_id: str) -> None:
    """Drive the graph off the request thread. run_research already turns failures into terminal
    states; this guard is a last resort so a crashed thread still marks the run failed."""
    try:
        run_research(question, run_id=run_id)
    except Exception:
        try:
            state = redis_store.load_state(run_id)
            if state and state.status not in _TERMINAL:
                state.status = RunStatus.failed
                redis_store.save_state(state)
        except Exception:
            pass


def _citations_valid(state: RunState) -> bool:
    """§4 final gate mirror: every citation must reference a real finding + a fetched source."""
    if state.report is None:
        return False
    finding_by_id = {f.id: f for f in state.findings}
    source_ids = {s.source_id for s in state.sources}
    for c in state.report.citations:
        f = finding_by_id.get(c.finding_id)
        if f is None or f.source_id not in source_ids:
            return False
    return True


@router.post("/research", response_model=ResearchAccepted, status_code=202)
def create_research(
    req: ResearchRequest,
    _rl: None = Depends(rate_limit),
    _principal: Principal = Depends(get_principal),
    _quota: None = Depends(enforce_quota),
) -> ResearchAccepted:
    if not redis_store.available():
        raise HTTPException(status_code=503, detail="state store unavailable")
    state = RunState(question=req.question, status=RunStatus.pending)
    redis_store.save_state(state)  # visible to GET before the thread's first write
    threading.Thread(
        target=_run_in_background, args=(req.question, state.run_id), daemon=True
    ).start()
    return ResearchAccepted(run_id=state.run_id, status=state.status)


@router.get("/research/{run_id}", response_model=RunView)
def get_research(run_id: str, _principal: Principal = Depends(require_run_access)) -> RunView:
    state = redis_store.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="not found")
    report = None
    if state.report is not None:
        report = ReportOut(
            body=state.report.body,
            citations=[
                CitationOut(marker=c.marker, finding_id=c.finding_id, source_url=c.source_url)
                for c in state.report.citations
            ],
            citations_valid=_citations_valid(state),
        )
    return RunView(
        run_id=state.run_id,
        question=state.question,
        status=state.status,
        num_sub_questions=len(state.plan.sub_questions) if state.plan else 0,
        num_findings=len(state.findings),
        num_sources=len(state.sources),
        cumulative_tokens=state.cumulative_tokens,
        searches_used=state.searches_used,
        fetches_used=state.fetches_used,
        report=report,
    )


@router.post("/research/{run_id}/cancel", status_code=202)
def cancel_research(run_id: str, _principal: Principal = Depends(require_run_access)) -> dict:
    if not redis_store.request_cancel(run_id):
        raise HTTPException(status_code=404, detail="not found or already finished")
    return {"run_id": run_id, "cancel_requested": True}


@router.get("/research/{run_id}/trace")
def get_trace(run_id: str, _principal: Principal = Depends(require_run_access)) -> dict:
    if not redis_store.exists(run_id):
        raise HTTPException(status_code=404, detail="not found")
    steps = redis_store.load_trace(run_id)
    return {"run_id": run_id, "steps": [s.model_dump(mode="json") for s in steps]}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


@router.get("/research/{run_id}/stream")
async def stream_research(
    run_id: str,
    request: Request,
    _principal: Principal = Depends(require_run_access),
) -> StreamingResponse:
    if not redis_store.exists(run_id):
        raise HTTPException(status_code=404, detail="not found")

    async def events():
        emitted = 0
        last_status = None
        for _ in range(2000):  # hard ceiling (~23 min) so a stream never hangs forever
            if await request.is_disconnected():
                break
            state = redis_store.load_state(run_id)
            if state is None:
                break
            while emitted < len(state.step_log):
                s = state.step_log[emitted]
                yield _sse({
                    "type": "step", "agent": s.agent, "output": s.output,
                    "tokens": s.tokens, "latency_ms": s.latency_ms,
                })
                emitted += 1
            if state.status != last_status:
                last_status = state.status
                yield _sse({"type": "status", "status": state.status})
            if state.status in _TERMINAL:
                yield _sse({
                    "type": "done", "status": state.status,
                    "num_findings": len(state.findings),
                    "citations": len(state.report.citations) if state.report else 0,
                })
                break
            await asyncio.sleep(0.7)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/")
def index() -> FileResponse:
    return FileResponse(_INDEX_HTML)
