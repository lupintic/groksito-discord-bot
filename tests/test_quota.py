"""
Tests for the simple per-user daily video quota (5/day).

Implemented in context.py and used by video_generation.py + _handle_generate_video.
The claim "5 videos por día" in the schema must be honest — these tests protect that.
"""

import pytest
from unittest.mock import patch

import groksito_discord.context as context_mod


def test_get_video_quota_default_zero():
    """Fresh user with no prior usage has 0 used, 5 remaining."""
    used, remaining = context_mod.get_video_quota(999999999)
    assert used == 0
    assert remaining == 5


def test_increment_video_quota_increments_and_returns_values():
    """Increment is optimistic (before the xAI call) and returns current (used, remaining)."""
    uid = 123456

    # Start clean
    used, rem = context_mod.get_video_quota(uid)
    assert used == 0
    assert rem == 5

    with patch.object(context_mod, "save_context", return_value=True) as mock_save:
        u1, r1 = context_mod.increment_video_quota(uid)
        assert u1 == 1
        assert r1 == 4
        assert mock_save.called

        u2, r2 = context_mod.increment_video_quota(uid)
        assert u2 == 2
        assert r2 == 3

    # get reflects the increments
    used, rem = context_mod.get_video_quota(uid)
    assert used == 2
    assert rem == 3


def test_quota_caps_at_zero_remaining(frozen_today):
    """When used reaches 5, remaining stays 0 (never negative)."""
    uid = 777777
    frozen_today("2026-06-03")

    with patch.object(context_mod, "save_context", return_value=True):
        for i in range(1, 6):
            used, rem = context_mod.increment_video_quota(uid)
            assert used == i
            assert rem == max(0, 5 - i)

        # One more (exceed)
        used, rem = context_mod.increment_video_quota(uid)
        assert used == 6
        assert rem == 0

    # get also reports 0 remaining
    used, rem = context_mod.get_video_quota(uid)
    assert used == 6
    assert rem == 0


def test_quota_resets_on_new_day(frozen_today):
    """Only today's count matters. A new calendar day gives fresh quota."""
    uid = 424242

    # Day 1: use 3
    frozen_today("2026-01-15")
    with patch.object(context_mod, "save_context", return_value=True):
        for _ in range(3):
            context_mod.increment_video_quota(uid)

    used, rem = context_mod.get_video_quota(uid)
    assert used == 3
    assert rem == 2

    # Next calendar day — should be fresh (0 used)
    frozen_today("2026-01-16")
    used, rem = context_mod.get_video_quota(uid)
    assert used == 0
    assert rem == 5

    # Using on the new day does not affect previous day (in-memory only keeps today on load, but here we test increment)
    with patch.object(context_mod, "save_context", return_value=True):
        context_mod.increment_video_quota(uid)

    used, rem = context_mod.get_video_quota(uid)
    assert used == 1
    assert rem == 4


def test_quota_is_per_user_isolated():
    """Different users have independent counters."""
    u1, u2 = 111, 222

    with patch.object(context_mod, "save_context", return_value=True):
        context_mod.increment_video_quota(u1)
        context_mod.increment_video_quota(u1)
        context_mod.increment_video_quota(u2)

    assert context_mod.get_video_quota(u1) == (2, 3)
    assert context_mod.get_video_quota(u2) == (1, 4)
    assert context_mod.get_video_quota(999) == (0, 5)
