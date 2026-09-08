"""Centralized telemetry sanitizer for Hermes Enhancer.

Every payload that reaches SQLite (event_buffer, sync_queue), learning
history, reports, or exception telemetry MUST pass through
``sanitize()`` first. The default replacement is the literal string
``"[REDACTED]"`` -- secrets are never hashed into telemetry (a hash of a
low-entropy secret is still an oracle).

Matching is case-insensitive and recursive over dict / list / tuple /
set and JSON-like nesting. Depth-capped and cycle-safe.
"""

from __future__ import annotations

from typing import Any

REDACTED = "[REDACTED]"

MAX_DEPTH = 32
MAX_STRING_LEN = 4000
MAX_ITEMS = 10000

# Normalized (lowercase, '-' -> '_') key tokens. A key is redacted when its
# normalized form equals a token or CONTAINS a token as a substring, so
# variants like "DB_PASSWORD", "X-Api-Key", "authorization" are covered.
SENSITIVE_TOKENS = frozenset({
    "password",
    "passwd",
    "pass",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "private_key",
    "credential",
    "credentials",
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "session",
    "session_id",
    "auth",
    "bearer",
    "jwt",
})


def is_sensitive_key(key: Any) -> bool:
    """Return True when a mapping key looks like it holds a secret."""
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_")
    for token in SENSITIVE_TOKENS:
        if token in normalized:
            return True
    return False


def _truncate(value: str) -> str:
    if len(value) > MAX_STRING_LEN:
        return value[:MAX_STRING_LEN] + "...[truncated]"
    return value


def sanitize(obj: Any, _depth: int = 0, _seen: Any = None) -> Any:
    """Return a deep-cleaned copy of *obj* with secrets replaced.

    Never mutates the input. Dictionaries whose key is sensitive have
    their whole value replaced with ``"[REDACTED]"`` without inspecting
    it further. Unknown / exotic objects are stringified (truncated).
    """
    if _depth > MAX_DEPTH:
        return REDACTED
    if _seen is None:
        _seen = set()
    if isinstance(obj, dict):
        if id(obj) in _seen:
            return REDACTED
        _seen.add(id(obj))
        cleaned: dict[Any, Any] = {}
        count = 0
        for key, value in obj.items():
            if count >= MAX_ITEMS:
                break
            count += 1
            if is_sensitive_key(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = sanitize(value, _depth + 1, _seen)
        _seen.discard(id(obj))
        return cleaned
    if isinstance(obj, (list, tuple)):
        if id(obj) in _seen:
            return REDACTED
        _seen.add(id(obj))
        items = obj[:MAX_ITEMS] if len(obj) > MAX_ITEMS else obj
        cleaned_list = [sanitize(v, _depth + 1, _seen) for v in items]
        _seen.discard(id(obj))
        return cleaned_list if isinstance(obj, list) else tuple(cleaned_list)
    if isinstance(obj, (set, frozenset)):
        if id(obj) in _seen:
            return REDACTED
        _seen.add(id(obj))
        cleaned_set = {sanitize(v, _depth + 1, _seen) for v in list(obj)[:MAX_ITEMS]}
        _seen.discard(id(obj))
        # Sanitized members may be unhashable only if exotic; fall back to str.
        try:
            return set(cleaned_set) if isinstance(obj, set) else frozenset(cleaned_set)
        except TypeError:
            return REDACTED
    if isinstance(obj, str):
        return _truncate(obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    # Exotic objects (datetimes, exceptions, custom classes): stringify.
    try:
        return _truncate(str(obj))
    except Exception:
        return REDACTED


def assert_no_plaintext(haystack: Any, secrets: list[str]) -> list[str]:
    """Return the subset of *secrets* found verbatim inside *haystack*.

    Test helper: empty list means redaction held.
    """
    import json as _json

    try:
        text = _json.dumps(haystack, default=str)
    except Exception:
        text = str(haystack)
    return [s for s in secrets if s and s in text]
