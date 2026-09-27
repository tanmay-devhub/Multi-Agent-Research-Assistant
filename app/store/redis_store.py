"""Durable run store: safe keys, TTLs, validated (de)serialization, locks, cancellation (§6, §7, §13).

  run:{run_id}:state   -> RunState JSON  (powers GET /research/{id})
  run:{run_id}:trace   -> step-log JSON  (powers GET /research/{id}/trace)
  run:{run_id}:lock    -> worker lock    (prevents concurrent duplicate execution)
  run:{run_id}:cancel  -> cancellation flag (durable; survives restart)

run_id must be 32 lowercase hex (uuid4().hex); no user-controlled fragment ever enters a key.
Serialization is JSON only — never pickle. Restored state is version-checked and Pydantic-validated
(incl. provenance invariants) before use; anything corrupt/incompatible fails safe (returns None).
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache

from app.config import settings
from app.schemas import STATE_SCHEMA_VERSION, TERMINAL_STATUSES, RunState, StepLog

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Atomic compare-and-delete so a worker only releases a lock it still owns.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


def _safe_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError("invalid run_id")
    return run_id


def state_key(run_id: str) -> str:
    return f"run:{_safe_run_id(run_id)}:state"


def trace_key(run_id: str) -> str:
    return f"run:{_safe_run_id(run_id)}:trace"


def _lock_key(run_id: str) -> str:
    return f"run:{_safe_run_id(run_id)}:lock"


def _cancel_key(run_id: str) -> str:
    return f"run:{_safe_run_id(run_id)}:cancel"


@lru_cache(maxsize=1)
def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def available() -> bool:
    try:
        return bool(_redis().ping())
    except Exception:
        return False


def save_state(state: RunState) -> None:
    r = _redis()
    ttl = settings.redis_state_ttl_seconds
    r.set(state_key(state.run_id), state.model_dump_json(), ex=ttl)
    r.set(
        trace_key(state.run_id),
        json.dumps([step.model_dump(mode="json") for step in state.step_log]),
        ex=ttl,
    )


def load_state(run_id: str) -> RunState | None:
    """Load + validate persisted state. Returns None on unknown / corrupt / version-mismatch."""
    try:
        key = state_key(run_id)
    except ValueError:
        return None
    raw = _redis().get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA_VERSION:
        return None  # incompatible / corrupt → fail safe, never execute (§6)
    try:
        state = RunState.model_validate(data)  # runs provenance invariants
    except Exception:
        return None
    # merge durable cancellation so a resumed run observes it in routing too
    if is_cancelled(run_id):
        state.cancel_requested = True
    return state


def load_trace(run_id: str) -> list[StepLog]:
    try:
        key = trace_key(run_id)
    except ValueError:
        return []
    raw = _redis().get(key)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list[StepLog] = []
    for item in items if isinstance(items, list) else []:
        try:
            out.append(StepLog.model_validate(item))
        except Exception:
            continue
    return out


def exists(run_id: str) -> bool:
    try:
        key = state_key(run_id)
    except ValueError:
        return False
    try:
        return bool(_redis().exists(key))
    except Exception:
        return False


# --- concurrency: per-run worker lock (§7) ------------------------------------

def acquire_lock(run_id: str) -> str | None:
    """SET NX EX. Returns an owner token on success, or None if another worker holds the run."""
    try:
        key = _lock_key(run_id)
    except ValueError:
        return None
    token = secrets.token_hex(16)
    try:
        return token if _redis().set(key, token, nx=True, ex=settings.redis_lock_ttl_seconds) else None
    except Exception:
        return None


def release_lock(run_id: str, token: str) -> None:
    try:
        key = _lock_key(run_id)
    except ValueError:
        return
    try:
        _redis().eval(_RELEASE_LUA, 1, key, token)
    except Exception:
        pass


# --- durable cancellation (§13) ----------------------------------------------

def request_cancel(run_id: str) -> bool:
    """Mark a run cancelled (separate key → no race with an in-flight worker's state writes).
    Returns False if the run is unknown or already terminal."""
    state = load_state(run_id)
    if state is None or state.status in TERMINAL_STATUSES:
        return False
    try:
        _redis().set(_cancel_key(run_id), "1", ex=settings.redis_state_ttl_seconds)
        return True
    except Exception:
        return False


def is_cancelled(run_id: str) -> bool:
    try:
        key = _cancel_key(run_id)
    except ValueError:
        return False
    try:
        return bool(_redis().exists(key))
    except Exception:
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
