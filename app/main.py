"""FastAPI entrypoint: research API + trace + live stream + minimal UI (Agent.MD §9, hardening §9)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.security import install_security
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production-safe startup config validation (§12): refuse to boot if unsafe in production.
    problems = settings.validate_startup()
    if problems and settings.is_production:
        raise RuntimeError("unsafe production configuration: " + "; ".join(problems))
    yield


_docs = settings.docs_enabled()
app = FastAPI(
    title="Multi-Agent Research Assistant (RAA)",
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
    lifespan=lifespan,
)
install_security(app)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Minimal by design — no internal config leaked."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: dependencies (Redis) are reachable."""
    if redis_ready():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


def redis_ready() -> bool:
    from app.store import redis_store

    return redis_store.available()
