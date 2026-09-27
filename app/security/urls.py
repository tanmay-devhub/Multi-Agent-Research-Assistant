"""URL normalization + validation (Agent hardening §1, §4).

One canonical form is used everywhere: dedup, per-domain caps, provenance, and citations. A URL that
cannot be normalized is rejected outright — malformed/ambiguous URLs never reach the fetch layer.
Normalization does NOT resolve DNS or decide reachability; that is the fetch layer's job (fetch.py).
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(raw: str) -> str | None:
    """Return a canonical http(s) URL, or None if it is malformed / disallowed.

    Rules: scheme must be http/https; no userinfo (credentials); host required; host lower-cased;
    default port dropped; fragment dropped; path defaults to '/'; length-capped.
    """
    from app.config import settings  # lazy to avoid import cycle

    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > settings.max_url_chars:
        return None
    try:
        parts = urlsplit(candidate)
    except (ValueError, UnicodeError):
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    # Reject credentials in the authority (user:pass@host) — a common SSRF/phishing vector.
    if "@" in parts.netloc:
        return None
    try:
        host = parts.hostname
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if not host:
        return None
    if any(c.isspace() for c in host):
        return None

    netloc = host.lower()
    if ":" in netloc:  # IPv6 literal must be bracketed in the authority
        netloc = f"[{netloc}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        if not (0 < port < 65536):
            return None
        netloc = f"{netloc}:{port}"

    path = parts.path or "/"
    normalized = urlunsplit((scheme, netloc, path, parts.query, ""))
    if len(normalized) > settings.max_url_chars:
        return None
    return normalized


def url_host(url: str) -> str:
    """Lower-cased host (no port) of a URL, or '' if unparseable — used for per-domain caps."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except (ValueError, UnicodeError):
        return ""
