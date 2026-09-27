"""SSRF-safe web fetch — the ONLY path to the outside network (Agent hardening §1).

Every fetch: normalizes the URL, resolves the host and rejects any non-public address (IPv4+IPv6),
disables automatic redirects and re-validates each hop manually, enforces explicit timeouts, caps
response bytes (post-decompression, so a compression bomb is stopped mid-stream), restricts content
types, sends a fixed User-Agent with no cookies/auth/proxy/netrc, and never executes JavaScript.

Returns a FetchOutcome — success or a sanitized failure reason — and never raises to callers, so a
hostile URL degrades to a state the graph can route around (failure-as-state).

Known residual: a TOCTOU DNS-rebind between our resolution and httpx's own connection resolve is not
fully closable through httpx's public API; see SECURITY.md. Set ALLOW_PRIVATE_NETWORKS=true only for
local development.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.security.urls import normalize_url

_REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class FetchOutcome:
    ok: bool
    reason: str                       # sanitized category, safe for traces
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    text: str = ""
    content_sha256: str | None = None
    elapsed_ms: int = 0               # wall time of the fetch (credibility signal)
    redirects: int = 0               # number of redirect hops followed


def _ip_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block
    if settings.allow_private_networks:
        return False
    # is_global is the strongest single check; the rest are explicit for clarity/defense in depth.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local          # includes 169.254.169.254 cloud metadata + IPv6 fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified         # 0.0.0.0 / ::
        or not ip.is_global
    )


def _resolve_ok(host: str, port: int | None) -> bool:
    """Resolve host to every A/AAAA record; reject if ANY resolved address is non-public.

    Rejecting when *any* record is blocked defends against split-horizon / mixed public+private DNS.
    Also catches malformed IP encodings (e.g. decimal/hex hosts) because they resolve to their
    real, then-validated address.
    """
    try:
        infos = socket.getaddrinfo(host, port or 0, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    return all(not _ip_blocked(ip) for ip in ips)


def _content_type_allowed(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith(settings.allowed_content_type_prefixes())


def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """Stream decompressed bytes up to `limit`; return None if the body exceeds it (bomb guard)."""
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) > limit:
            return None
    return bytes(buf)


def secure_fetch(url_raw: str) -> FetchOutcome:
    normalized = normalize_url(url_raw)
    if normalized is None:
        return FetchOutcome(ok=False, reason="blocked_url")

    timeout = httpx.Timeout(
        connect=settings.fetch_connect_timeout,
        read=settings.fetch_read_timeout,
        write=settings.fetch_write_timeout,
        pool=settings.fetch_pool_timeout,
    )
    headers = {
        "User-Agent": settings.fetch_user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "gzip, deflate",
    }
    start = time.monotonic()
    deadline = start + settings.fetch_total_timeout
    current = normalized
    hops = 0

    def _ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        # trust_env=False → ignore ambient proxies and .netrc creds; cookies never persisted.
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False,
            headers=headers, max_redirects=0, verify=True,
        ) as client:
            for _hop in range(settings.max_redirects + 1):
                if time.monotonic() > deadline:
                    return FetchOutcome(ok=False, reason="timeout", final_url=current)

                parsed = httpx.URL(current)
                if not _resolve_ok(parsed.host, parsed.port):
                    return FetchOutcome(ok=False, reason="blocked_host", final_url=current)

                try:
                    with client.stream("GET", current) as resp:
                        if resp.status_code in _REDIRECT_CODES:
                            location = resp.headers.get("location")
                            if not location:
                                return FetchOutcome(ok=False, reason="http_error",
                                                    final_url=current, status_code=resp.status_code)
                            nxt = normalize_url(urljoin(current, location))
                            if nxt is None:
                                return FetchOutcome(ok=False, reason="blocked_url", final_url=current)
                            current = nxt
                            hops += 1
                            continue

                        if resp.status_code >= 400:
                            return FetchOutcome(ok=False, reason="http_error",
                                                final_url=current, status_code=resp.status_code)

                        content_type = resp.headers.get("content-type", "")
                        if not _content_type_allowed(content_type):
                            return FetchOutcome(ok=False, reason="bad_content_type",
                                                final_url=current, status_code=resp.status_code,
                                                content_type=content_type)

                        body = _read_capped(resp, settings.max_response_bytes)
                        if body is None:
                            return FetchOutcome(ok=False, reason="too_large", final_url=current,
                                                status_code=resp.status_code, content_type=content_type)
                        if not body:
                            return FetchOutcome(ok=False, reason="empty", final_url=current,
                                                status_code=resp.status_code, content_type=content_type)

                        encoding = resp.encoding or "utf-8"
                        try:
                            text = body.decode(encoding, errors="replace")
                        except (LookupError, TypeError):
                            text = body.decode("utf-8", errors="replace")
                        return FetchOutcome(
                            ok=True, reason="ok", final_url=current,
                            status_code=resp.status_code, content_type=content_type,
                            text=text, content_sha256=hashlib.sha256(body).hexdigest(),
                            elapsed_ms=_ms(), redirects=hops,
                        )
                except (httpx.TimeoutException,):
                    return FetchOutcome(ok=False, reason="timeout", final_url=current)
                except httpx.HTTPError:
                    return FetchOutcome(ok=False, reason="network_error", final_url=current)

            return FetchOutcome(ok=False, reason="too_many_redirects", final_url=current)
    except Exception:
        # Absolute last resort: never let the fetch layer raise into the pipeline.
        return FetchOutcome(ok=False, reason="network_error", final_url=current)
