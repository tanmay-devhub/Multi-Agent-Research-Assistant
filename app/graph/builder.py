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

from app.agents import planner, researcher, writer
from app.agents.researcher import SearchFn, _default_search
from app.graph import supervisor
from app.llm import TokenMeter
from app.schemas import Plan, Report, RunState, RunStatus, StepLog
from app.tools.search import get_search_fn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


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
        try:
            findings = researcher.research(sub, search_fn=search, config=meter.config)
            output = f"{len(findings)} findings"
        except Exception as exc:  # failures become state, never crash the run (§6)
            findings = []
            output = f"error: {type(exc).__name__}: {exc}"
        step = StepLog(
            agent="researcher",
            input=sub.text,
            output=output,
            tools_called=["search"],
            tokens=meter.total_tokens,
            latency_ms=_ms_since(t0),
        )
        return {
            "findings": state.findings + findings,
            "research_passes": {**state.research_passes, sub.id: passes + 1},
            "cumulative_tokens": state.cumulative_tokens + meter.total_tokens,
            "step_log": state.step_log + [step],
            "updated_at": _utcnow(),
        }

    def review_node(state: RunState) -> dict:
        return supervisor.review(state)

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
                output = f"{len(report.citations)} citations"
            except Exception as exc:  # writer failure → partial report + failed state (§6), never crashes
                failed = True
                report = Report(
                    question=state.question,
                    body=f"Report generation failed ({type(exc).__name__}: {exc}). "
                    f"{len(state.findings)} findings with provenance were gathered but could not be written up.",
                    citations=[],
                )
                output = f"error: {type(exc).__name__}: {exc}"
        if failed:
            status = RunStatus.failed
        elif supervisor.budget_exceeded(state):
            status = RunStatus.budget_exceeded
        else:
            status = RunStatus.completed
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
    graph.add_node("writer", writer_node)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        supervisor.route_after_planner,
        {supervisor.RESEARCHER: "researcher", supervisor.WRITER: "writer"},
    )
    graph.add_edge("researcher", "review")
    graph.add_conditional_edges(
        "review",
        supervisor.route_after_review,
        {supervisor.RESEARCHER: "researcher", supervisor.WRITER: "writer"},
    )
    graph.add_edge("writer", END)
    return graph.compile()


_TERMINAL = {RunStatus.completed, RunStatus.failed, RunStatus.budget_exceeded}


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


def _drive(graph, state: RunState, persist: bool) -> RunState:
    """Stream the graph, persisting the full state after every super-step (§8)."""
    save = _persister(persist)
    last = state
    if save:
        save(last)
    for chunk in graph.stream(state, {"recursion_limit": 50}, stream_mode="values"):
        last = _as_state(chunk)
        if save:
            save(last)
    return last


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
    return _drive(graph, state, persist)


def resume_research(run_id: str, search_fn: SearchFn | None = None) -> RunState | None:
    """Resume a persisted run from its last checkpoint. Returns None if the run is unknown.
    Completed runs are returned as-is (idempotent); otherwise the graph continues without replay —
    each node no-ops on work already reflected in the restored state (§8)."""
    from app.store import redis_store

    state = redis_store.load_state(run_id)
    if state is None:
        return None
    if state.status in _TERMINAL:
        return state
    graph = build_graph(search_fn)
    return _drive(graph, state, persist=True)
