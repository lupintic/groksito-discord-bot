"""
Compatibility shim.

All image generation logic has been centralized and modernized in:
    media/image_handler.py

Recommended imports:
    from groksito_discord.media.image_handler import ...
    or
    from groksito_discord.media_tools import _handle_generate_image

This file only re-exports for any code that was importing directly from the old location.
"""

from __future__ import annotations

from .media.image_handler import (
    _handle_generate_image,
    _tool_generate_image,
    soften_image_prompt,
    _enhance_prompt_for_api,
)

import logging
logger = logging.getLogger("groksito.media_tools")
