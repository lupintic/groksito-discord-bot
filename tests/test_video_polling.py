"""Tests for xAI video generation polling timeout and delivery TTL alignment."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from groksito_discord.config import settings
from groksito_discord.media.delivery import _request_ttl_for_operation
from groksito_discord.media.video_handler import (
    _poll_for_video_completion,
    _video_poll_max_wait_seconds,
)


def test_video_poll_default_at_least_ten_minutes():
    """xAI SDK default poll timeout is 10 minutes; bot must not give up at 5 min."""
    assert _video_poll_max_wait_seconds() >= 600


def test_video_request_ttl_exceeds_poll_timeout():
    """Delivery registration must outlive polling so deliver_from_request can succeed."""
    poll_max = _video_poll_max_wait_seconds()
    ttl = _request_ttl_for_operation("video")
    assert ttl > poll_max


def test_video_poll_scales_for_heavy_jobs():
    """720p / 15s requests need more wall time than 480p / 6s."""
    light = _video_poll_max_wait_seconds(duration=6, resolution="480p")
    heavy = _video_poll_max_wait_seconds(duration=15, resolution="720p")
    assert heavy >= light


@pytest.mark.asyncio
async def test_poll_parses_status_from_202_accepted():
    """xAI often returns 202 while pending; status may still be in the JSON body."""
    resp_pending = MagicMock()
    resp_pending.status_code = 202
    resp_pending.content = b'{"status":"pending"}'
    resp_pending.json.return_value = {"status": "pending"}

    resp_done = MagicMock()
    resp_done.status_code = 202
    resp_done.content = b'{"status":"done","video":{"url":"https://cdn.x.ai/v.mp4"}}'
    resp_done.json.return_value = {
        "status": "done",
        "video": {"url": "https://cdn.x.ai/v.mp4"},
    }

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[resp_pending, resp_done])

    with patch("groksito_discord.media.video_handler.asyncio.sleep", new_callable=AsyncMock):
        status, data = await _poll_for_video_completion(
            client, "req-1", "key", max_wait_seconds=60, poll_interval=0.01
        )

    assert status == "succeeded"
    assert data["video"]["url"] == "https://cdn.x.ai/v.mp4"


@pytest.mark.asyncio
async def test_poll_times_out_after_max_wait(monkeypatch):
    """Poll loop must not return timeout before max_wait_seconds elapses."""
    resp_pending = MagicMock()
    resp_pending.status_code = 202
    resp_pending.content = b'{"status":"pending"}'
    resp_pending.json.return_value = {"status": "pending"}

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp_pending)

    clock = {"t": 0.0}

    def fake_time():
        return clock["t"]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", fake_time)

    async def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(
        "groksito_discord.media.video_handler.asyncio.sleep",
        fake_sleep,
    )

    status, _ = await _poll_for_video_completion(
        client, "req-1", "key", max_wait_seconds=30, poll_interval=10.0
    )

    assert status == "timeout"
    assert clock["t"] >= 30


def test_settings_video_poll_max_wait_seconds_configurable():
    assert getattr(settings, "video_poll_max_wait_seconds", 0) >= 600