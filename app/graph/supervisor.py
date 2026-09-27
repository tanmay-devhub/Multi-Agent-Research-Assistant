"""Supervisor: budget checks, per-sub-question review, and routing (Agent.MD §3.3, §5).

Pure functions over RunState. The supervisor is NOT an LLM here — routing and review are
deterministic code, which is exactly where budgets belong (§5, §10.4) and keeps token cost low.
Review is a heuristic: a sub-question is 'answered' if it produced findings (which always carry
provenance); otherwise the reviewer sends it back exactly once before marking it failed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.schemas import RunState, StepLog, SubQuestionStatus

RESEARCHER = "researcher"
WRITER = "writer"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def elapsed_seconds(state: RunState) -> float:
    return (_utcnow() - state.created_at).total_seconds()


def budget_exceeded(state: RunState) -> bool:
    """Hard caps: total tokens or wall-clock. Sub-question and per-search caps are enforced
    upstream (planner caps the count; researcher caps queries)."""
    return (
        state.cumulative_tokens >= settings.max_total_tokens
        or elapsed_seconds(state) >= settings.wall_clock_seconds
    )


def route_after_planner(state: RunState) -> str:
    if state.plan is None or not state.plan.sub_questions:
        return WRITER
    if budget_exceeded(state):
        return WRITER
    if state.current_index >= len(state.plan.sub_questions):
        return WRITER  # research already complete (resume case) — go straight to the writer
    return RESEARCHER


def review(state: RunState) -> dict:
    """Decide answered / retry / failed for the current sub-question; return RunState updates.

    Advances the cursor on answered or failed. On retry it leaves the cursor put and spends the
    single allowed send-back (§5). Routing (route_after_review) then re-runs the same index.
    """
    plan = state.plan
    index = state.current_index
    sub = plan.sub_questions[index]
    got = [f for f in state.findings if f.sub_question_id == sub.id]
    used = state.send_backs_used.get(sub.id, 0)

    updated_plan = plan.model_copy(deep=True)
    send_backs = dict(state.send_backs_used)
    completed = list(state.completed_sub_questions)
    new_index = index

    if got:
        updated_plan.sub_questions[index].status = SubQuestionStatus.answered
        completed.append(sub.id)
        new_index = index + 1
        note = f"answered ({len(got)} findings)"
    elif used < settings.reviewer_send_backs and not budget_exceeded(state):
        send_backs[sub.id] = used + 1
        note = "send-back (no findings; retry 1x)"
    else:
        updated_plan.sub_questions[index].status = SubQuestionStatus.failed
        completed.append(sub.id)
        new_index = index + 1
        note = "failed (no findings; send-back exhausted)"

    step = StepLog(agent="supervisor", input=sub.text, output=note)
    return {
        "plan": updated_plan,
        "current_index": new_index,
        "send_backs_used": send_backs,
        "completed_sub_questions": completed,
        "step_log": state.step_log + [step],
        "updated_at": _utcnow(),
    }


def route_after_review(state: RunState) -> str:
    if budget_exceeded(state):
        return WRITER
    if state.plan is not None and state.current_index < len(state.plan.sub_questions):
        return RESEARCHER
    return WRITER
