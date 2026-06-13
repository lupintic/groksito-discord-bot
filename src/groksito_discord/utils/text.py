"""
Centralized text and link extraction utilities.

This module was introduced in Phase 2 of the refactoring to eliminate
duplicated URL/link extraction logic that previously existed in both
`conversation.py` (as private helpers) and inline inside `client.py`
(on_message context harvesting).

Functions:
- extract_urls_from_text
- extract_x_links
- extract_image_urls_from_text

All three preserve **exact** previous behavior, cleaning rules,
priority ordering, and output as the original implementations.

Intended for use by conversation handling, context building, and
any future text processing that needs robust http(s) URL extraction
with punctuation stripping and domain-specific filtering.
"""

from __future__ import annotations

import re


def extract_urls_from_text(text: str) -> list[str]:
    """
    General-purpose URL extractor from plain text (supports X links, image URLs, etc. for robust replies).

    Extracts all http/https URLs, cleaned of trailing punctuation.
    Used as the foundation for both image-specific and X-link-specific extraction.

    This is the key mechanism for X/Twitter links (and other external links) in replies:
    We extract generally so links in the referenced message (the one user replied to)
    can be reliably surfaced in HIGH PRIORITY context for the model.
    """
    if not text:
        return []

    url_pattern = r'https?://[^\s<>"\']+'
    candidates = re.findall(url_pattern, text)

    seen: set[str] = set()
    clean_urls: list[str] = []

    for raw in candidates:
        clean = raw.rstrip('.,;:!?\'"()[]{}')
        if clean and clean not in seen:
            seen.add(clean)
            clean_urls.append(clean)

    return clean_urls


def extract_x_links(text: str) -> list[str]:
    """
    Extracts X/Twitter post links from text.

    Matches common patterns:
    - https://x.com/username/status/123456...
    - https://twitter.com/... (historical example)
    - Also handles x.com/i/web/status etc.

    This enables proactive surfacing of X posts when the user replies to a message
    containing one and asks about its content ("de qué habla este tweet?", etc.).
    """
    if not text:
        return []

    all_urls = extract_urls_from_text(text)
    x_patterns = ("x.com/", "twitter.com/")

    x_links = []
    for u in all_urls:
        u_lower = u.lower()
        if any(p in u_lower for p in x_patterns):
            # Prefer status links but keep any x.com link as useful signal
            x_links.append(u)

    return x_links


def extract_image_urls_from_text(text: str) -> list[str]:
    """
    Extracts candidate image URLs from plain text (e.g. message.content).

    Refactored to reuse the general
    extract_urls_from_text helper. Behavior is identical for images.

    Supports vision + general link extraction for rich referenced context.
    """
    all_urls = extract_urls_from_text(text)
    if not all_urls:
        return []

    priority_substrings = ("grok-imagine", "x.ai")
    image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif")

    prioritized: list[str] = []
    others: list[str] = []
    seen: set[str] = set()

    for url in all_urls:
        url_lower = url.lower()
        is_priority = any(substr in url_lower for substr in priority_substrings)

        path_part = url_lower.split("?", 1)[0].split("#", 1)[0]
        has_image_ext = path_part.endswith(image_extensions)

        if is_priority or has_image_ext:
            if url not in seen:
                seen.add(url)
                if is_priority:
                    prioritized.append(url)
                else:
                    others.append(url)

    return prioritized + others


def filter_unreliable_vision_urls(urls: list[str]) -> list[str]:
    """Filter out image URLs from known transient/unreliable CDNs and proxies (e.g. X/Twitter pbs.twimg.com previews, Discord link embed proxies).

    These frequently cause 404 (or header/region/proxy issues) when the xAI Responses API backend attempts to fetch them for vision,
    especially for recent tweets, multi-image posts, X link cards, or certain regions/accounts. Discord embed
    thumbnails for x.com links (and other external link previews) are the common source when harvesting vision for "que piensas de esto"
    style mentions (direct or via recent referent on @mention).

    The filter runs in harvest + as last-mile in input builder.

    Skipping them allows graceful degradation: the x.com (or other link) URL remains visible in the user text,
    has_x_link_intent / text signals cause x_search (or web_search) to be offered, and the native tools can surface
    post content and media info reliably from server side (no client fetch of the CDN/proxy url).

    This keeps good (stable) image URLs like direct Discord attachments (cdn.discordapp.com/attachments), grok-generated, or
    user-hosted original image links (e.g. imgur, unsplash, or bare .jpg in text) working for native vision.
    """
    if not urls:
        return []

    unsafe_domains = ("pbs.twimg.com", "video.twimg.com")
    # Discord external *link preview* proxies (for tweets, YT cards, web links etc). These are transient,
    # often header-sensitive or short-lived when fetched server-to-server by vision backends.
    # Do NOT match real user attachments: cdn.discordapp.com/attachments/... are stable and desired.
    discord_proxy_markers = ("images-ext", "discordapp.net/external")
    safe: list[str] = []
    for u in urls:
        if not u:
            continue
        try:
            ul = u.lower()
            if any(d in ul for d in unsafe_domains):
                continue
            if any(m in ul for m in discord_proxy_markers) or "/external/" in ul:
                continue
            safe.append(u)
        except Exception:
            # be permissive on weird data
            safe.append(u)
    return safe
