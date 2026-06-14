"""
Compatibility shim.

All image editing logic has been centralized and modernized in:
    media/image_handler.py

Recommended:
    from groksito_discord.media.image_handler import _handle_edit_image
    or via media_tools.

This file only provides backward-compatible re-exports.
"""

from __future__ import annotations

from .image_handler import (
    _handle_edit_image,
    _tool_edit_image,
)

import logging
logger = logging.getLogger("groksito.media_tools")
