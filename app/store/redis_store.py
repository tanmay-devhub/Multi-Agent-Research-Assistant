"""Durable run store under an explicit key scheme (Agent.MD §8).

  run:{run_id}:state  -> RunState JSON  (powers GET /research/{id})
  run:{run_id}:trace  -> step-log JSON  (powers GET /research/{id}/trace)

The state mirror is written after every graph super-step, so callers see live progress and a crash
leaves a readable snapshot to resume from. Resume mechanics (do-not-replay) live in the graph:
each node is idempotent, so re-invoking with the restored state continues instead of redoing work.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import settings
from app.schemas import RunState, StepLog


def state_key(run_id: str) -> str:
    return f"run:{run_id}:state"


def trace_key(run_id: str) -> str:
    return f"run:{run_id}:trace"


@lru_cache(maxsize=1)
def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def available() -> bool:
    """True if Redis is reachable — callers degrade to no-persistence when it isn't."""
    try:
        return bool(_redis().ping())
    except Exception:
        return False


def save_state(state: RunState) -> None:
    r = _redis()
    r.set(state_key(state.run_id), state.model_dump_json())
    r.set(
        trace_key(state.run_id),
        json.dumps([step.model_dump(mode="json") for step in state.step_log]),
    )


def load_state(run_id: str) -> RunState | None:
    raw = _redis().get(state_key(run_id))
    return RunState.model_validate_json(raw) if raw else None


def load_trace(run_id: str) -> list[StepLog]:
    raw = _redis().get(trace_key(run_id))
    return [StepLog.model_validate(item) for item in json.loads(raw)] if raw else []


def exists(run_id: str) -> bool:
    return bool(_redis().exists(state_key(run_id)))
