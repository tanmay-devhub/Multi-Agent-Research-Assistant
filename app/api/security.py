"""API security: auth/authorization hooks, rate-limit/quota seams, middleware, error handling (§9).

Authentication and per-user ownership are intentionally abstracted behind small hooks that default
to permissive single-user behavior, so real auth/quotas can be dropped in without touching route
logic. Knowing a run_id is NEVER authorization — `authorize_run_access` is the ownership gate.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from app.config import settings
from app.security import redact

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # UI is self-contained (inline script/style, links only); no external origins, no framing.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


# --- authentication / authorization abstraction --------------------------------

@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Default is anonymous single-user; replace get_principal for real auth."""
    id: str = "anonymous"


async def get_principal(request: Request) -> Principal:
    """HOOK: resolve the caller (API key / JWT / session). Default: anonymous.
    Wire real authentication here; routes depend on this, not on run_id knowledge."""
    return Principal()


def authorize_run_access(principal: Principal, run_id: str) -> bool:
    """HOOK: ownership check. Default: allow (single-user). Replace with a principal↔run mapping so a
    caller cannot read/stream another caller's run just by knowing its id."""
    return True


async def require_run_access(run_id: str, principal: Principal = Depends(get_principal)) -> Principal:
    if not authorize_run_access(principal, run_id):
        raise HTTPException(status_code=403, detail="forbidden")
    return principal


async def rate_limit(request: Request) -> None:
    """HOOK: per-user/IP rate limiting. No-op by default — plug a limiter (e.g. Redis token bucket)."""
    return None


async def enforce_quota(principal: Principal = Depends(get_principal)) -> None:
    """HOOK: daily / token / spend / max-active-run quotas. No-op by default."""
    return None


# --- middleware ----------------------------------------------------------------

class RequestContextMiddleware:
    """Pure-ASGI middleware (streaming-safe, unlike BaseHTTPMiddleware): assigns a request id,
    enforces the body-size cap from Content-Length, and stamps security headers on every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > settings.api_max_body_bytes:
                    await _error_response(413, "request body too large", request_id)(scope, receive, send)
                    return
            except ValueError:
                await _error_response(400, "invalid content-length", request_id)(scope, receive, send)
                return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                hdrs = MutableHeaders(raw=message.setdefault("headers", []))
                for k, v in _SECURITY_HEADERS.items():
                    if k not in hdrs:
                        hdrs[k] = v
                hdrs["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --- error handling ------------------------------------------------------------

def _error_response(status: int, detail: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": redact(detail), "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return _error_response(exc.status_code, str(exc.detail), _request_id(request))


async def validation_exception_handler(request: Request, exc) -> JSONResponse:
    # Do not echo raw input back (may be large / sensitive); keep it generic.
    return _error_response(422, "invalid request", _request_id(request))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces, provider errors, or secrets to clients.
    return _error_response(500, "internal server error", _request_id(request))


def install_security(app) -> None:
    """Attach middleware and sanitized exception handlers to the FastAPI app."""
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list())

    origins = settings.cors_origins()
    if origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
