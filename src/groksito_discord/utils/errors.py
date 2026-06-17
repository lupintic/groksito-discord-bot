"""
Consistent error handling for auxiliary (non-fatal) bot operations.

Use these helpers when optional features must never crash the bot, but failures
should still be visible in logs (with correlation IDs when available).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, TypeVar

from .correlation import cid_prefix

T = TypeVar("T")


def format_tool_execution_error(
    tool_name: str,
    exc: BaseException,
    *,
    round_num: int | None = None,
    arg_keys: list[str] | None = None,
) -> str:
    """
    Build a structured tool error string for the model and logs.

    Includes exception type, message, and lightweight execution context so the
    model can reason about retries or alternate tools.
    """
    exc_type = type(exc).__name__
    parts = [
        f"Tool '{tool_name}' failed ({exc_type}): {exc}",
    ]
    if round_num is not None:
        parts.append(f"round={round_num}")
    if arg_keys:
        parts.append(f"arg_keys={arg_keys}")
    return " | ".join(parts)


def log_auxiliary_failure(
    logger: logging.Logger,
    operation: str,
    exc: BaseException,
    *,
    feature: str = "",
    level: int = logging.WARNING,
    degraded: bool = True,
) -> None:
    """Log a non-fatal auxiliary failure with optional degraded-status marker."""
    cid_p = cid_prefix()
    feat = f"[{feature}] " if feature else ""
    status = " [degraded]" if degraded else ""
    logger.log(
        level,
        f"{cid_p}{feat}{operation} failed (non-fatal){status}: {exc}",
        exc_info=level >= logging.ERROR,
    )


@contextmanager
def safe_auxiliary(
    operation: str,
    *,
    logger: logging.Logger,
    feature: str = "",
    level: int = logging.WARNING,
    reraise: bool = False,
):
    """
    Context manager for optional features that must not crash the bot.

    Logs failures at WARNING (or ERROR when level is set) and optionally re-raises.
    """
    try:
        yield
    except Exception as exc:
        log_auxiliary_failure(
            logger,
            operation,
            exc,
            feature=feature,
            level=level,
        )
        if reraise:
            raise


def run_auxiliary(
    fn: Callable[[], T],
    *,
    fallback: T,
    operation: str,
    logger: logging.Logger,
    feature: str = "",
    level: int = logging.WARNING,
) -> T:
    """Run a sync auxiliary callable; return fallback on failure."""
    try:
        return fn()
    except Exception as exc:
        log_auxiliary_failure(
            logger,
            operation,
            exc,
            feature=feature,
            level=level,
        )
        return fallback


def is_image_fetch_404_error(err: BaseException | str, *, has_images: bool) -> bool:
    """
    Detect xAI vision backend image-fetch 404 / stale-URL failures.

    Extracted so the retry path can be unit-tested without a full LLM call.
    """
    if not has_images:
        return False
    err_str = str(err)
    err_lower = err_str.lower()
    return bool(
        "fetching image failed" in err_lower
        or ("404" in err_lower and ("image" in err_lower or "not found" in err_lower))
        or "unrecoverable data loss" in err_lower
    )