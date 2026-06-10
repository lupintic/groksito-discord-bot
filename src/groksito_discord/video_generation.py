"""
Compatibility shim.

All video generation logic (T2V + I2V) has been centralized and modernized in:
    media/video_handler.py

Recommended imports:
    from groksito_discord.media.video_handler import ...
    or
    from groksito_discord.media_tools import _generate_video_schema, _handle_generate_video

This file only re-exports for any code that was importing directly from the old location.
"""

from __future__ import annotations

from .media.video_handler import (
    _generate_video_schema,
    _handle_generate_video,
    _tool_generate_video,
    _enhance_video_prompt,
    _poll_for_video_completion,
)

import logging
logger = logging.getLogger("groksito.media_tools")
