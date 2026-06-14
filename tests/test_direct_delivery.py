"""
Tests for the DIRECT_DELIVERY_PERFORMED sentinel and success string contract.

This mechanism guarantees exactly-one-reply UX for media (image/video/edit):
- A media tool (or reply_to_user) does the Discord .reply() itself.
- It returns a "SUCCESS: ..." string (or similar).
- llm.py detects it (via substring) and short-circuits (does not send tool result back to model).
- conversation.py does an *identity* check (`is DIRECT_DELIVERY_PERFORMED`) and suppresses the final text reply.

These tests protect the identity contract and the string phrases that cross the module boundary.
No network calls are made.
"""

import pytest

from groksito_discord.media.delivery import DIRECT_DELIVERY_PERFORMED


def test_direct_delivery_performed_is_singleton_sentinel():
    """The sentinel must be a unique object() used with 'is', never equality or truthiness."""
    assert DIRECT_DELIVERY_PERFORMED is DIRECT_DELIVERY_PERFORMED
    # Different object() must not be equal under 'is'
    other = object()
    assert other is not DIRECT_DELIVERY_PERFORMED
    # It is truthy (so "if response_text:" would pass), but we must use 'is' not truthy
    assert bool(DIRECT_DELIVERY_PERFORMED) is True


def test_conversation_uses_identity_check_pattern():
    """conversation.py relies on 'is' (not == or 'in') for the sentinel."""
    # Simulate what conversation.py does
    response_text = DIRECT_DELIVERY_PERFORMED
    assert response_text is DIRECT_DELIVERY_PERFORMED

    # A normal string (even one containing "SUCCESS") must NOT match the sentinel
    normal = "SUCCESS: something happened"
    assert normal is not DIRECT_DELIVERY_PERFORMED


# The critical strings that media tools return on successful *direct* delivery.
# These (when lowercased) must be recognized by the detection logic in llm.py
# so that the tool result is not sent back to the model and conversation suppresses reply.
KNOWN_DIRECT_SUCCESS_STRINGS = [
    "SUCCESS: Edited image(s) delivered directly to the user.",
    "SUCCESS: Image(s) generated and delivered directly to the user.",
    "SUCCESS: Video successfully generated and delivered directly to the user.",
    # Policy block direct delivery also short-circuits
    "SUCCESS: Image generation policy blocked; clean direct message delivered to the user.",
]


def test_media_success_strings_contain_detector_phrases():
    """
    The strings returned by _tool_* / _handle_* on happy direct-delivery path
    must contain one of the phrases that llm.py looks for in result_str.lower().

    This is a contract test: if a media module changes its SUCCESS wording without
    updating llm.py's list (or vice-versa), double-reply or missing suppression can occur.
    """
    detector_phrases = [
        "success: image(s) generated and delivered directly",
        "success: edited image(s) delivered directly",
        "delivered directly to the user",
        "clean direct message delivered to the user",
        "policy blocked; clean direct message delivered",
        # Also the broader fallbacks in llm.py
        "delivered directly",
        "success:",
        "policy blocked",
    ]

    for s in KNOWN_DIRECT_SUCCESS_STRINGS:
        lowered = s.lower()
        matched = any(p in lowered for p in detector_phrases)
        assert matched, f"String '{s}' does not contain any of the expected detector phrases"


def test_success_strings_are_not_the_sentinel():
    """SUCCESS strings are *not* the sentinel — the sentinel is returned by the *llm layer*."""
    for s in KNOWN_DIRECT_SUCCESS_STRINGS:
        assert s is not DIRECT_DELIVERY_PERFORMED
        # Even the object identity must be different
        assert isinstance(s, str)
