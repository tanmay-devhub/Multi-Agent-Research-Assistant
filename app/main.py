"""FastAPI entrypoint. Research endpoints arrive in Phase 5 (Agent.MD §9)."""
from __future__ import annotations

from fastapi import FastAPI

from app.config import settings

app = FastAPI(title="Multi-Agent Research Assistant (RAA)")


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "provider_chain": settings.provider_chain()}
