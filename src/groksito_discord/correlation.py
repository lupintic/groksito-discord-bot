"""
Lightweight correlation ID support for tracing individual user interactions.

Uses stdlib contextvars so the ID is automatically available throughout
the async call chain for a given Discord message (no manual passing required
in most cases).

The ID is a short 8-char hex string (from uuid4) for readability in logs.
"""

from __future__ import annotations

import contextvars
import uuid

# Context variable holding the current correlation ID (per async task/context).
# Default empty so logs degrade gracefully if not set.
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def generate_correlation_id() -> str:
    """Generate a short, human-readable correlation ID."""
    return uuid.uuid4().hex[:8]


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current context/task."""
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Return the current correlation ID, or '' if none has been set."""
    return _correlation_id.get()


def cid_prefix() -> str:
    """
    Return a log prefix like 'cid=abc12345 ' (with trailing space) or empty string.
    Use at the start of log messages for easy grepping/filtering.
    """
    cid = get_correlation_id()
    if cid:
        return f"cid={cid} "
    return ""
