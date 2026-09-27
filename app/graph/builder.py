"""LangGraph wiring: nodes = specialists, edges = supervisor routing (Agent.MD §3.1).

RunState is the shared source of truth AND the graph's state schema; nodes return partial
updates. Execution is sequential, so accumulating list fields are updated by returning the full
new list (no reducers needed). Every node appends a StepLog entry — that log is the trace (§9).
A terminal state always produces a partial-but-valid result and never crashes (§5, §6).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from app.agents import contradiction, planner, researcher, writer
from app.agents.researcher import SearchFn, _default_search
from app.config import settings
from app.graph import supervisor
from app.llm import TokenMeter
from app.schemas import TERMINAL_STATUSES, Plan, Report, RunState, RunStatus, StepLog
from app.tools.search import get_search_fn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _validate_report_provenance(report: Report, state: RunState):
    """Final citation gate (§4): keep a citation only if it chains
    citation -> real finding -> finding.source_id -> a fetched SourceRef. Cap total citations.
    Orphan / invented / mismatched citations are dropped deterministically.
    """
    from app.schemas import Report as _Report

    finding_by_id = {f.id: f for f in state.findings}
    source_ids = {s.source_id for s in state.sources}
    kept = []
    for c in report.citations:
        finding = finding_by_id.get(c.finding_id)
        if finding is None or finding.source_id not in source_ids:
            continue
        kept.append(c)
        if len(kept) >= settings.max_citations:
            break
    return _Report(question=report.question, body=report.body, citations=kept,
                   generated_at=report.generated_at)


def _surface_contradictions(report: Report, state: RunState) -> Report:
    """Append a deterministic `[CONFLICTING]` section listing each cross-source conflict with both
    source URLs, so contradictions are surfaced regardless of what the writer LLM did."""
    if not state.contradictions:
        return report
    url_by_id = {s.source_id: s.url for s in state.sources}
    lines = ["", "## Conflicting sources"]
    for c in state.contradictions:
        ua = url_by_id.get(c.source_id_a, c.source_id_a)
        ub = url_by_id.get(c.source_id_b, c.source_id_b)
        lines.append(f"- [CONFLICTING] {c.claim_a} ({ua}) vs. {c.claim_b} ({ub})")
    return Report(
        question=report.question,
        body=report.body + "\n" + "\n".join(lines),
        citations=report.citations,
        generated_at=report.generated_at,
    )


def build_graph(search_fn: SearchFn | None = None):
    """Compile the research graph. `search_fn` is injected into the researcher; when omitted it
    resolves to live Tavily search if a key is configured, else degrades to zero findings."""
    search: SearchFn = search_fn or get_search_fn() or _default_search

    def planner_node(state: RunState) -> dict:
        if state.plan is not None:
            return {}  # idempotent: plan already exists (resume) — do not re-run
        meter = TokenMeter()
        t0 = time.perf_counter()
        try:
            plan = planner.plan(state.question, config=meter.config)
            output = f"{len(plan.sub_questions)} sub-questions"
        except Exception as exc:  # planner failure → empty plan; routes to writer (§6), never crashes
            plan = Plan(question=state.question)
            output = f"error: {type(exc).__name__}: {exc}"
        step = StepLog(
            agent="planner",
            input=state.question,
            output=output,
            tokens=meter.total_tokens,
            latency_ms=_ms_since(t0),
        )
        return {
            "plan": plan,
            "status": RunStatus.researching,
            "cumulative_tokens": state.cumulative_tokens + meter.total_tokens,
            "step_log": state.step_log + [step],
            "updated_at": _utcnow(),
        }

    def researcher_node(state: RunState) -> dict:
        sub = state.plan.sub_questions[state.current_index]
        passes = state.research_passes.get(sub.id, 0)
        sends = state.send_backs_used.get(sub.id, 0)
        if passes > sends:
            return {}  # idempotent: already researched this attempt (crash between researcher & review)
        meter = TokenMeter()
        t0 = time.perf_counter()
        new_findings: list = []
        new_sources: list = []
        searches = fetches = 0
        # Hard wall-clock interrupt: bound this pass to the run's remaining time (§5).
        remaining = settings.wall_clock_seconds - supervisor.elapsed_seconds(state)
        deadline = time.monotonic() + max(0.0, remaining)
        try:
            result = researcher.research(sub, search_fn=search, config=meter.config, deadline=deadline)
            new_findings, new_sources = result.findings, result.sources
            searches, fetches = result.searches, result.fetches
            output = f"{len(new_findings)} findings"
        except Exception as exc:  # failures become state, never crash the run (§6)
            output = f"error: {type(exc).__name__}"  # sanitized: no raw exception text in trace
        # cap total findings deterministically (DoS/cost guard, §3/§5)
        room = max(0, settings.max_findings_total - len(state.findings))
        new_findings = new_findings[:room]
        step = StepLog(
            agent="researcher",
            input=sub.text,
            output=output,
            tools_called=["search"],
            tokens=meter.total_tokens,
            latency_ms=_ms_since(t0),
        )
        return {
            "findings": state.findings + new_findings,
            "sources": state.sources + new_sources,
            "research_passes": {**state.research_passes, sub.id: passes + 1},
            "searches_used": state.searches_used + searches,   # monotonic budget counters
            "fetches_used": state.fetches_used + fetches,
            "cumulative_tokens": state.cumulative_tokens + meter.total_tokens,
            "step_log": state.step_log + [step],
            "updated_at": _utcnow(),
        }

    def review_node(state: RunState) -> dict:
        return supervisor.review(state)

    def analyze_node(state: RunState) -> dict:
        # Post-research contradiction detection. Skip the LLM call when it can't/shouldn't run
        # (cancelled, over budget, or <2 findings) — budget-before-expensive-op (§5).
        if (
            state.cancel_requested
            or supervisor.budget_exceeded(state)
            or len(state.findings) < 2
        ):
            return {}
        meter = TokenMeter()
        t0 = time.perf_counter()
        try:
            found = contradiction.detect(state.findings, config=meter.config)
            output = f"{len(found)} contradictions"
        except Exception as exc:  # never crash the run (§6)
            found = []
            output = f"error: {type(exc).__name__}"
        step = StepLog(
            agent="analyze", input=f"{len(state.findings)} findings", output=output,
            tokens=meter.total_tokens, latency_ms=_ms_since(t0),
        )
        return {
            "contradictions": found,
            "cumulative_tokens": state.cumulative_tokens + meter.total_tokens,
            "step_log": state.step_log + [step],
            "updated_at": _utcnow(),
        }

    def writer_node(state: RunState) -> dict:
        if state.report is not None:
            return {}  # idempotent: report already written (resume) — do not re-run
        meter = TokenMeter()
        t0 = time.perf_counter()
        failed = False
        if not state.findings:
            report = Report(
                question=state.question,
                body="No findings with provenance were gathered, so no cited report could be produced.",
                citations=[],
            )
            output = "0 citations (no findings)"
        else:
            try:
                report = writer.write(state.plan, state.findings, config=meter.config)
                report = _validate_report_provenance(report, state)
                output = f"{len(report.citations)} citations"
            except Exception as exc:  # writer failure → partial report + failed state (§6), never crashes
                failed = True
                report = Report(
                    question=state.question,
                    body=f"Report generation failed ({type(exc).__name__}). "
                    f"{len(state.findings)} findings with provenance were gathered but could not be written up.",
                    citations=[],
                )
                output = f"error: {type(exc).__name__}"  # sanitized: no raw exception text in trace
        report = _surface_contradictions(report, state)  # deterministic [CONFLICTING] section (§ contradictions)
        if failed:
            status = RunStatus.failed
        elif state.cancel_requested:
            status = RunStatus.cancelled            # durable cancellation → partial report (§13)
        else:
            status = supervisor.budget_status(state) or RunStatus.completed
        step = StepLog(
            agent="writer",
            input=f"{len(state.findings)} findings",
            output=output,
            tokens=meter.total_tokens,
            latency_ms=_ms_since(t0),
        )
        return {
            "report": report,
            "status": status,
            "cumulative_tokens": state.cumulative_tokens + meter.total_tokens,
            "step_log": state.step_log + [step],
            "updated_at": _utcnow(),
        }

    graph = StateGraph(RunState)
    graph.add_node("planner", planner_node)
    graph.add_node("researcher", researcher_node)
    graph.add_node("review", review_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("writer", writer_node)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        supervisor.route_after_planner,
        {supervisor.RESEARCHER: "researcher", supervisor.ANALYZE: "analyze"},
    )
    graph.add_edge("researcher", "review")
    graph.add_conditional_edges(
        "review",
        supervisor.route_after_review,
        {supervisor.RESEARCHER: "researcher", supervisor.ANALYZE: "analyze"},
    )
    graph.add_edge("analyze", "writer")
    graph.add_edge("writer", END)
    return graph.compile()


def _as_state(chunk) -> RunState:
    return chunk if isinstance(chunk, RunState) else RunState(**chunk)


def _persister(persist: bool):
    """Return a save callback that mirrors state to Redis after each node, or None when Redis is
    unavailable / persistence is off — a run never fails just because the store is down."""
    if not persist:
        return None
    from app.store import redis_store

    if not redis_store.available():
        return None
    return redis_store.save_state


def _finalize_cancelled(state: RunState, save) -> RunState:
    """Stop new work and land in a durable `cancelled` terminal state (§13). Findings are kept."""
    state.cancel_requested = True
    if state.status not in TERMINAL_STATUSES:
        state.status = RunStatus.cancelled
        state.updated_at = _utcnow()
    if save:
        save(state)
    return state


def _drive(graph, state: RunState, persist: bool) -> RunState:
    """Stream the graph, persisting the full state after every super-step (§8). Honors cancellation
    between steps so a cancelled run stops before the next expensive node."""
    from app.store import redis_store

    save = _persister(persist)
    run_id = state.run_id

    def cancelled() -> bool:
        return save is not None and redis_store.is_cancelled(run_id)

    last = state
    if cancelled():
        return _finalize_cancelled(last, save)
    if save:
        save(last)
    for chunk in graph.stream(state, {"recursion_limit": 50}, stream_mode="values"):
        last = _as_state(chunk)
        if save:
            save(last)
        if last.status not in TERMINAL_STATUSES and cancelled():
            return _finalize_cancelled(last, save)
    return last


def _run_locked(graph, state: RunState, persist: bool) -> RunState:
    """Run under a per-run Redis lock so concurrent workers/resumes cannot duplicate work,
    double-charge tokens, or clobber state (§7). Without a store, runs in-memory (no lock)."""
    if not persist:
        return _drive(graph, state, persist=False)
    from app.store import redis_store

    if not redis_store.available():
        return _drive(graph, state, persist=False)
    token = redis_store.acquire_lock(state.run_id)
    if token is None:
        # another worker holds this run → do not duplicate; return current persisted state
        return redis_store.load_state(state.run_id) or state
    try:
        return _drive(graph, state, persist=True)
    finally:
        redis_store.release_lock(state.run_id, token)


def run_research(
    question: str,
    search_fn: SearchFn | None = None,
    run_id: str | None = None,
    persist: bool = True,
) -> RunState:
    """Run the full graph end-to-end and return the final RunState (report + trace + spend).
    State is mirrored to Redis after every node when a store is available."""
    graph = build_graph(search_fn)
    state = RunState(question=question, status=RunStatus.planning)
    if run_id:
        state.run_id = run_id
    return _run_locked(graph, state, persist)


def resume_research(run_id: str, search_fn: SearchFn | None = None) -> RunState | None:
    """Resume a persisted run from its last checkpoint. Returns None if the run is unknown.
    Completed runs are returned as-is (idempotent); otherwise the graph continues without replay —
    each node no-ops on work already reflected in the restored state (§8). Budgets/counters are
    carried from the restored state and never reset (§5)."""
    from app.store import redis_store

    state = redis_store.load_state(run_id)
    if state is None:
        return None
    if state.status in TERMINAL_STATUSES:
        return state
    graph = build_graph(search_fn)
    return _run_locked(graph, state, persist=True)
