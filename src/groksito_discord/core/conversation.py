"""
Conversation handling, activation detection, vision harvesting, and Groksito invocation.

Key responsibilities:
- Activation decision (mentions, replies, explicit visual intent + X-link support)
- Rich referenced context building (including reply chain traversal)
- Vision image harvesting (attachments + text-extracted URLs + chain fallback)
- Invocation of the LLM stack (llm + tools)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..utils.correlation import cid_prefix

from .. import context
from .safety import safe_reply
# No custom memory (fully removed for maximum nativeness)
from ..media.delivery import DIRECT_DELIVERY_PERFORMED
from ..llm.llm_utils import _detect_image_creation_intent

# Centralized text utilities (Phase 2). Aliases preserve the original private
# names used throughout this file so that no call sites inside conversation.py
# needed to change.
from ..utils.text import (
    extract_urls_from_text as _extract_urls_from_text,
    extract_x_links as _extract_x_links,
    extract_image_urls_from_text as _extract_image_urls_from_text,
)

# Centralized intent/keyword data (Phase 5). We import with the original names
# (including the private _has_* aliases) so that the rest of this file and all
# its internal call sites require zero modifications.
from .intent import (
    STRONG_DIRECTED_KEYWORDS,
    GENERAL_REPLY_INQUIRY_KEYWORDS,
    _has_strong_directed_reply_intent,
    _has_recent_referent_intent,
)

logger = logging.getLogger("groksito.conversation")

# Max age (seconds) for auto-including images from the recent channel history for vision
# on direct @mentions with referent language ("arriba", "esto", "la imagen", etc.).
# Only very fresh images have reliably-valid Discord signed attachment URLs (or fresh embed previews).
# Older ones are skipped for native vision to avoid 404s when the xAI backend fetches;
# the message text + get_recent_context tool (when offered) + user description remain available.
_RECENT_VISION_MAX_AGE_SECONDS: int = 15 * 60  # 15 minutes is conservative for "recent visual referent"

# URL / link extraction helpers were moved to src/groksito_discord/utils/text.py
# (Phase 2 centralization). They are re-exported above via aliases so the rest
# of this file (and its call sites) required zero other modifications.
# The implementations are now the single source of truth.

# STRONG_DIRECTED_KEYWORDS / GENERAL_REPLY_INQUIRY_KEYWORDS and the two
# _has_*_intent functions were moved to intents.py (Phase 5).
# They are imported (with original names) at the top of this file.


async def _resolve_referenced_and_activation(
    message: Any,
    client_user: Any,
    author_display: str,
) -> tuple[Any | None, bool, bool, bool, bool, bool]:
    """
    Determines activation intent and fetches the referenced message if present.
    Returns: (referenced_message, is_reply_to_bot, explicit_visual_reply_intent, is_reply_continuation, has_x_link_intent, has_image_creation_intent)

    ACTIVATION POLICY (strict, post-bugfix):
    - Direct @mention ΓåÆ activate
    - Reply to one of *our* messages ΓåÆ activate
    - Reply to *another user* ΓåÆ only activate on strong directed signals
      (explicit visual follow-up keywords or the STRONG_DIRECTED_KEYWORDS list
       which includes targeted questions about the referenced item + bot name mentions).

    The broad GENERAL_REPLY_INQUIRY_KEYWORDS only affect *context quality* for
    already-activated turns. They must never cause the bot to wake up on ordinary
    user-to-user replies.

    has_x_link_intent (broadened) is still passed downstream so the LLM and vision
    harvester can provide rich referenced context + chain traversal when appropriate.
    It is also set on direct @mentions (no reply reference) when the text contains
    recent referent / inquiry language so that "what the user / the image the user posted"
    cases get proper recent context and gated vision.

    has_image_creation_intent is STRICT (gen/edit/transform/video-from-img commands only).
    It is used to decide whether to offer the token-heavy generate_image/edit_image tools.
    It is deliberately narrower than explicit_visual_reply_intent (which also covers analysis).
    Mere presence of images (in reply or attachment) does NOT set creation intent.

    The cid (if set via correlation contextvar) is included in all logs for this turn.
    """
    cid_p = cid_prefix()
    is_reply_to_bot = False
    explicit_visual_reply_intent = False
    is_reply_continuation = False
    has_x_link_intent = False   # Always initialize to avoid UnboundLocalError on non-replies
    referenced = None

    # Always compute referent/inquiry signals from the current message text.
    # This allows direct @mentions (no Discord reply) to benefit from recent context,
    # recent vision harvesting, and has_x_link_intent for "what the user / the image the user posted" questions.
    current_text = message.content or ""
    current_text_lower = current_text.lower()
    has_recent_referent = _has_recent_referent_intent(current_text)
    has_general_reply_inquiry = any(kw in current_text_lower for kw in GENERAL_REPLY_INQUIRY_KEYWORDS)

    is_mentioned_now = client_user in getattr(message, "mentions", [])

    if message.reference and message.reference.message_id:
        is_reply_continuation = True
        logger.debug(f"{cid_p}[Reply] Reply detected to message_id={message.reference.message_id} from {author_display}")

        try:
            referenced = await message.channel.fetch_message(message.reference.message_id)
            if getattr(referenced, "author", None) and referenced.author.id == client_user.id:
                is_reply_to_bot = True
                logger.info(f"{cid_p}[Reply] Direct reply to bot's previous message")
            else:
                logger.info(f"{cid_p}[Reply] Reply to another user's message (bot mentioned or activated)")

            # Log attachments in referenced for visibility
            ref_atts = getattr(referenced, "attachments", []) or []
            if ref_atts:
                image_count = sum(1 for a in ref_atts if getattr(a, "content_type", "").startswith("image/"))
                logger.info(f"{cid_p}[Reply] Referenced message has {len(ref_atts)} attachments ({image_count} images)")

        except Exception as fetch_err:
            logger.warning(f"{cid_p}[Reply] Could not fetch referenced message: {fetch_err}")

        # Check current message for visual follow-up intent (even on replies)
        text_lower = current_text_lower

        # Detect inquiry about referenced content.
        # We maintain TWO levels:
        # - STRONG (for activation on replies to other users) ΓåÆ uses STRONG_DIRECTED_KEYWORDS
        # - GENERAL (for rich context / chain traversal once already activated) ΓåÆ uses GENERAL list
        has_specific_x = any(kw in text_lower for kw in STRONG_DIRECTED_KEYWORDS)

        # has_x_link_intent passed downstream = specific OR general.
        # This ensures that *when we do activate*, the LLM gets rich referenced context
        # even for broader inquiries. The broadening no longer affects wake-up decisions.
        has_x_link_intent = has_specific_x or has_general_reply_inquiry

        explicit_image_reply_keywords = [
            # Edit/transform style operations on a specific image
            "edita esta", "edita la", "edit├í esta", "edit├í la",
            "transforma esta", "transforma la", "convierte esta", "convierte la",
            "pasa esta a", "pasa la a", "cambia esta a", "cambia la a",
            "redibuja esta", "redibuja la",
            "meme con esta", "meme con la", "haz un meme con esta",
            "genera un estilo con esta", "haz un estilo con esta",
            # Analysis / describe the referenced image (broadened for reliability)
            "qu├⌐ ves en esta", "qu├⌐ ves en la", "analiza esta", "analiza la",
            "describe", "qu├⌐ ves", "cu├⌐ntame de esta", "qu├⌐ hay en esta",
            "qu├⌐ ves aqu├¡", "qu├⌐ ves en la foto", "qu├⌐ ves en la imagen",
            "explica esta", "explica la", "explica esto", "describe esta", "describe la",
            # Common casual references to the image being replied to
            "qu├⌐ es esto", "esto qu├⌐ es", "qu├⌐ es esta", "mira esta", "mira la", "mira esto",
            "opina de esta", "qu├⌐ opinas de esta", "qu├⌐ piensas de esta", "esta imagen", "esta foto",
            # Reference to previous / bot-generated image (critical for text-URL case)
            "esa imagen", "esas im├ígenes", "la imagen", "esa foto", "la foto",
            "la que generaste", "la que mandaste", "la anterior", "la de antes",
            "la foto anterior", "el video anterior", "la generada", "la del bot",
            "basado en esta", "usando esta", "con esta imagen", "con esta foto",
            "la que respond├¡", "la url de la", "esa url",
            # Video / animation from image
            "video de esta", "video de la", "haz un video de esta", "haz un video de la",
            "genera un video de esta", "genera un video de la", "crea un video de esta",
            "anima esta", "anima la", "convierte esta en video", "convierte la en video",
            "video con esta foto", "haz video de la que respond├¡",
        ]
        if any(kw in text_lower for kw in explicit_image_reply_keywords):
            explicit_visual_reply_intent = True
            logger.info(f"{cid_p}[Reply] Visual follow-up intent detected in reply")

        if has_x_link_intent:
            logger.info(f"{cid_p}[Reply] Reply inquiry intent detected (user appears to be asking about the referenced message content)")

    # For direct mentions (even without using Discord reply), honor recent referent / inquiry language.
    # This powers recent context, gated recent vision for images, and tool signals for questions like
    # "what the user said" or "what does the image the user posted mean?".
    if is_mentioned_now and (has_general_reply_inquiry or has_recent_referent):
        has_x_link_intent = True
        if not (message.reference and message.reference.message_id):
            logger.info(f"{cid_p}[Mention] Recent referent / inquiry language on direct mention (ensuring recent context + possible vision for referent)")

    # === STRICT REPLY ACTIVATION LOGIC (bugfix for user-to-user reply spam) ===
    # Design principle (conservative):
    # - Mention in current message ΓåÆ always wake
    # - Reply directly to one of *our* previous messages ΓåÆ always wake
    # - Reply to *someone else* ΓåÆ only wake on *very strong* directed signals:
    #     * explicit visual intent (the long "edita esta / video de esta / la que generaste" list)
    #     * strong targeted inquiry phrases (the STRONG_DIRECTED_KEYWORDS)
    #     * explicit bot name mention in the reply text
    #
    # The broad GENERAL_REPLY_INQUIRY_KEYWORDS ("esto", "el anterior", "qu├⌐ opinas", etc.)
    # are deliberately *excluded* from activation. They only influence context richness
    # after we have already decided to respond.
    if is_mentioned_now:
        should_activate = True
        logger.info(f"{cid_p}[Activation] Direct @mention in message from {author_display}")
    elif is_reply_continuation:
        if is_reply_to_bot:
            should_activate = True
            logger.info(f"{cid_p}[Activation] Direct reply to bot's own previous message from {author_display}")
        elif explicit_visual_reply_intent:
            should_activate = True
            logger.info(f"{cid_p}[Activation] Reply to other + explicit visual intent (image/video follow-up) from {author_display}")
        elif _has_strong_directed_reply_intent(message.content or ""):
            should_activate = True
            logger.info(f"{cid_p}[Activation] Reply to other + strong directed inquiry / bot name from {author_display}")
        else:
            should_activate = False
            logger.info(f"{cid_p}[Groksito] Ignoring reply from {author_display} to another user (plain user-to-user reply, no mention, not to bot, no strong directed signal)")
    else:
        should_activate = False
        if not is_mentioned_now:
            logger.info(f"{cid_p}[Groksito] Ignoring non-reply message from {author_display} (no mention)")

    # Compute STRICT image creation/edit intent from CURRENT message text.
    # This is independent of the broad explicit_visual_reply_intent (which is used for
    # activation + harvest chain + analysis cases). creation_intent controls only
    # whether we advertise generate_image/edit_image etc to avoid tool bloat in mixed cases.
    user_text = getattr(message, "content", "") or ""
    has_image_creation_intent = _detect_image_creation_intent(user_text)
    if has_image_creation_intent:
        logger.info(f"{cid_p}[Intent] Image creation/edit intent detected (will offer gen/edit tools)")

    return referenced, is_reply_to_bot, explicit_visual_reply_intent, is_reply_continuation, has_x_link_intent, has_image_creation_intent


async def _build_referenced_context(referenced: Any) -> dict[str, Any]:
    """Builds rich structured context from a referenced Discord message.
    This is now treated as high-priority context for replies.

    Now also extracts general external links and X/Twitter links
    from the message text (in addition to images). This dramatically improves the
    bot's ability to answer questions about X posts that the user replied to.
    """
    cid_p = cid_prefix()  # ensure cid prefix is always defined for logs in this helper (correlation contextvar is set upstream)
    if not referenced:
        return {}

    try:
        raw_content = (getattr(referenced, "content", "") or "").strip()
        # Smarter truncation for long referenced messages (storage). The injection cap
        # in llm_input.py is the one that directly affects prompt size (currently 200 chars).
        if len(raw_content) > 700:
            ref_content = raw_content[:450] + " ... " + raw_content[-150:]
        else:
            ref_content = raw_content
        ref_author = getattr(getattr(referenced, "author", None), "display_name", "unknown")

        attachments = []
        image_urls = []
        for att in getattr(referenced, "attachments", []) or []:
            att_info = {
                "url": getattr(att, "url", ""),
                "filename": getattr(att, "filename", ""),
                "content_type": getattr(att, "content_type", ""),
            }
            attachments.append(att_info)
            if getattr(att, "content_type", "").startswith("image/"):
                image_urls.append(att.url)

        # Extract images + general links (especially X posts) from referenced text.
        # Critical for X link replies: we do structured extraction here (model may miss raw links).
        ref_text_for_extract = getattr(referenced, "content", "") or ""

        # Images (vision path, preserved)
        image_urls_from_text = _extract_image_urls_from_text(ref_text_for_extract)
        for u in image_urls_from_text:
            if u not in image_urls:
                image_urls.append(u)

        # General external links + X-specific links for better reply robustness
        all_links = _extract_urls_from_text(ref_text_for_extract)
        x_links = _extract_x_links(ref_text_for_extract)

        context = {
            "author": ref_author,
            "content": ref_content,
            "attachments": attachments,
            "has_attachments": len(attachments) > 0,
            "image_urls": image_urls,
            # New fields for link-aware replies (X posts, YouTube, general links, etc.)
            "external_links": all_links,
            "x_links": x_links,
            "has_x_links": len(x_links) > 0,
        }

        if image_urls:
            logger.info(f"{cid_p}[Reply] High-priority: {len(image_urls)} image(s) found in referenced message")

        if image_urls_from_text:
            logger.info(f"{cid_p}[Reply] Enriched referenced context with {len(image_urls_from_text)} text-extracted image URL(s) (no attachments)")

        if x_links:
            logger.info(f"{cid_p}[Reply] Detected {len(x_links)} X/Twitter link(s) in referenced message content")

        if all_links and not x_links:
            logger.info(f"{cid_p}[Reply] Enriched referenced context with {len(all_links)} external link(s)")

        return context
    except Exception as e:
        logger.warning(f"{cid_p}Failed to build referenced context: {e}")
        return {}


async def _fetch_reply_chain_context(
    message: Any,
    max_depth: int = 3,
    require_images: bool = False,
) -> list[dict]:
    """
    Intelligently traverses the reply chain backwards to collect relevant context
    (authors, text, external links like YouTube, images, X links).

    Now supports deeper *text* chain walking (not just images) so that when a user
    mentions Groksito (with or without formal reply) and asks about "the video the
    user posted", "what that person said", or similar recent referent, the model
    can see the actual source content up the thread.

    Early stopping still applies for efficiency (substantial text or images when required).
    Returns list of context dicts (from _build_referenced_context), most recent first.
    """
    cid_p = cid_prefix()  # ensure cid prefix for logs (fixes NameError; reuses correlation system)
    contexts = []
    current_ref_id = getattr(message.reference, "message_id", None) if message.reference else None
    depth = 0

    while current_ref_id and depth < max_depth:
        try:
            ref_msg = await message.channel.fetch_message(current_ref_id)
            ctx = await _build_referenced_context(ref_msg)
            if ctx:
                contexts.append(ctx)
                logger.info(f"{cid_p}[ReplyChain] Fetched chain level {depth+1}: has_images={bool(ctx.get('image_urls'))}")

            # Stop if we have images and they were required, or if we have substantial text
            if require_images and ctx.get("image_urls"):
                break
            if ctx.get("content") and len(ctx.get("content", "")) > 100:
                break

            # Move to parent
            parent_ref = getattr(ref_msg, "reference", None)
            current_ref_id = getattr(parent_ref, "message_id", None) if parent_ref else None
            depth += 1
        except Exception as e:
            logger.warning(f"{cid_p}[ReplyChain] Failed to fetch chain level {depth}: {e}")
            break

    if contexts:
        logger.info(f"{cid_p}[ReplyChain] Collected {len(contexts)} messages from reply chain (depth traversed: {depth})")

    return contexts


async def _harvest_vision_images(
    message: Any,
    referenced: Any | None,
    explicit_visual_reply_intent: bool,
    is_reply_continuation: bool = False,
    has_x_link_intent: bool = False,  # also used to decide chain traversal for link-heavy replies
    is_mentioned: bool = False,
    user_text: str = "",
) -> list[str]:
    """Harvests image URLs for Vision from current message, referenced message, reply chain if needed,
    and (lightweight) recent channel messages when directly @mentioned + the query refers to a recent
    user post / image (no Discord reply used).

    The recent-channel path is intentionally gated and small-cap so it only activates when needed
    (direct address + clear reference language like "el usuario", "la imagen", "arriba", "el post anterior",
    "what the user posted", "the image", etc.). This lets Grok use reasoning over recent context
    while staying lightweight.

    Attachment images from the *referenced* message are now harvested on any reply_continuation
    (once the strict activation guard allowed us to reach this point). This greatly improves
    reliability when users reply to messages containing images (friend posts, screenshots, etc.)
    and wake the bot via mention or strong directed signals ΓÇö even if they didn't use one of the
    explicit "qu├⌐ ves en esta / edita esta" phrases.

    explicit_visual_reply_intent + has_x_link_intent still gate the more specific "text-extracted
    previous Grok image URLs" (the "la que generaste" cases) and deep chain traversal.

    Image *creation* intent (stricter) is separate and only controls tool schema offering upstream.
    """
    cid_p = cid_prefix()  # ensure cid prefix for logs in helper (correlation contextvar; prevents NameError)
    image_urls: list[str] = []

    # Current message attachments (rare for vision replies, but supported)
    for att in getattr(message, "attachments", []) or []:
        if getattr(att, "content_type", "").startswith("image/"):
            u = att.url
            if u not in image_urls:
                image_urls.append(u)
                logger.debug(f"{cid_p}[Vision] Image URL from attachment (current msg): {u[:70]}...")

    # Direct referenced message: attachments + text extraction (primary path)
    #
    # IMPORTANT FIX: We now harvest *attachment* images from the referenced message
    # whenever we are in a reply continuation (once activation has already passed the
    # strict guard in resolve/client). This fixes the case where a user replies to a
    # friend/other message that contains an image (and @mentions or uses strong signal)
    # but the exact visual keywords were not used ΓåÆ explicit_visual was False.
    # Previously this caused "bot responds without the image context".
    #
    # Text-extracted Grok-generated image URLs (grok-imagine etc) remain gated behind
    # explicit visual / x-intent for the "la que generaste" follow-up cases.
    if referenced and is_reply_continuation:
        # Attachment images from referenced ΓÇö include for any activated reply.
        # (Activation policy + STRONG keywords already protect from random user-to-user.)
        for att in getattr(referenced, "attachments", []) or []:
            if getattr(att, "content_type", "").startswith("image/"):
                u = att.url
                if u not in image_urls:
                    image_urls.append(u)
                    logger.debug(f"{cid_p}[Vision] Image URL from attachment (referenced): {u[:70]}...")

        # Text-based extraction (bot image links in the referenced text) ΓÇö keep stricter.
        if is_reply_continuation and (explicit_visual_reply_intent or has_x_link_intent):
            ref_text = getattr(referenced, "content", "") or ""
            text_urls = _extract_image_urls_from_text(ref_text)
            if text_urls:
                logger.info(f"{cid_p}[Reply] Extracted {len(text_urls)} image URL(s) from referenced_text")
                logger.info(f"{cid_p}[Vision] Vision activated thanks to text-extracted image URL(s) from reply (bot delivered via text+URL, not attachment)")
            for u in text_urls:
                if u not in image_urls:
                    image_urls.append(u)
                    logger.debug(f"{cid_p}[Vision] Image URL from referenced_text: {u[:70]}...")

    # Intelligent reply chain traversal for visual follow-ups (or X/link intent) when direct ref lacks useful content
    # Also trigger chain traversal when the user is asking about links in the reply.
    # This helps when the direct referenced message is short but the parent has the X post link.
    if is_reply_continuation and (explicit_visual_reply_intent or has_x_link_intent) and not image_urls:
        logger.info(f"{cid_p}[Reply] No images in direct referenced message ΓÇö traversing reply chain for visual context")
        try:
            chain_contexts = await _fetch_reply_chain_context(
                message, max_depth=3, require_images=True
            )
            for ctx in chain_contexts:
                for url in ctx.get("image_urls", []):
                    if url not in image_urls:
                        image_urls.append(url)
                        # Source may be attachment or text-extracted in ancestor (enriched by build)
                        logger.debug(f"{cid_p}[ReplyChain] Found additional image from chain: {url[:70]}...")
                        # For deeper logging we could inspect raw, but keep the primary path focused
        except Exception as chain_err:
            logger.warning(f"{cid_p}[Reply] Chain traversal for images failed: {chain_err}")

    # === Lightweight recent channel images for direct @mentions (no reply reference) ===
    # Only when the user directly mentions Groksito and the query clearly refers to a recent
    # user post, "the user", "the image the user posted", "arriba", "el anterior", etc.
    # This lets Grok reason about "which user/post" using the recent summary + actual vision
    # for the images, without always pulling recent images or doing heavy history.
    # Cap is small (2) and only on addressed turns with reference signals.
    if not image_urls and is_mentioned and user_text:
        if _has_recent_referent_intent(user_text):
            try:
                ch = getattr(getattr(message, "channel", None), "id", None)
                if ch:
                    from ..context import get_recent_channel_messages
                    recent_msgs = get_recent_channel_messages(ch, limit=8)
                    added = 0
                    now = time.time()
                    for m in reversed(recent_msgs):  # most recent first
                        msg_ts = m.get("ts") or 0
                        age_ok = (now - msg_ts) <= _RECENT_VISION_MAX_AGE_SECONDS if msg_ts else False

                        # Prefer stored image_urls (from attachments or prior embed capture).
                        # These are the risky ones: Discord cdn attachment URLs are signed and expire;
                        # embed previews (even after our filter) can also go 404. Only use if the source
                        # message is very recent.
                        if age_ok:
                            for u in (m.get("image_urls") or []):
                                if u and u not in image_urls:
                                    image_urls.append(u)
                                    added += 1
                                    logger.debug(f"{cid_p}[Vision] Recent channel image from mention reference: {u[:70]}...")
                                if added >= 2:
                                    break
                        else:
                            if (m.get("image_urls") or []) and msg_ts:
                                logger.debug(f"{cid_p}[Vision] Skipped stale image(s) from recent msg (age >15m) to avoid 404 on xAI vision fetch")

                        if added >= 2:
                            break

                        # Also extract original image URLs from the recent message *text content*.
                        # These are usually stable publisher URLs (imgur etc.) the user literally pasted,
                        # not Discord-signed ones. Apply the age gate too for safety/consistency, but
                        # they are lower risk.
                        if added < 2 and age_ok:
                            content = m.get("content") or ""
                            for u in _extract_image_urls_from_text(content):
                                if u and u not in image_urls:
                                    image_urls.append(u)
                                    added += 1
                                    logger.debug(f"{cid_p}[Vision] Recent channel image (text-extracted original) from mention reference: {u[:70]}...")
                                if added >= 2:
                                    break
                        if added >= 2:
                            break
                    if added:
                        logger.info(f"{cid_p}[Vision] Added {added} recent channel image(s) for direct mention + recent visual reference (lightweight)")
            except Exception as recent_vision_err:
                logger.debug(f"{cid_p}[Vision] Recent channel image pull skipped: {recent_vision_err}")

    # Filter transient/unreliable preview images (pbs.twimg.com, Discord external link proxies etc)
    # that commonly 404 (or fail header/region checks) when xAI vision backend fetches them.
    # This protects "que piensas de esto [x.com link]" and similar referent cases (direct mention or reply):
    # we still surface the link text + has_x_link_intent so the model can (and will) use x_search
    # (or web_search) for reliable post content + media. Good images (attachments, grok gens, stable user image links)
    # are preserved for native vision.
    raw_count = len(image_urls)
    from ..utils.text import filter_unreliable_vision_urls
    image_urls = filter_unreliable_vision_urls(image_urls)
    if len(image_urls) < raw_count:
        logger.info(f"{cid_p}[Vision] Filtered {raw_count - len(image_urls)} unreliable image URL(s) (X/Twitter previews or Discord link-embed proxies) to prevent 404 on vision; using text + x_search fallback")

    if image_urls:
        logger.info(f"{cid_p}[Vision] Total image URLs harvested for this turn: {len(image_urls)} (reply_continuation={is_reply_continuation}, mentioned={is_mentioned})")

    return image_urls[:5]  # Safety cap


async def _invoke_groksito(
    message: Any,
    referenced: Any | None,
    referenced_context: dict | None,
    author_display: str,
    is_meta_convo: bool,
    explicit_visual_reply_intent: bool,
    is_reply_continuation: bool = False,
    has_x_link_intent: bool = False,  # signal that user is asking about links/X posts in the reply
    is_reply_to_bot: bool = False,    # Direct reply to one of our previous messages (affects some context decisions)
    has_image_creation_intent: bool = False,  # STRICT: only gen/edit/transform commands; controls heavy media tool schemas
    is_mentioned: bool = False,       # Direct mention of the bot (affects [R:] ref injection + light decision tool offering for on-demand recent context)
) -> None:
    """
    Invokes the Groksito conversational response (llm + tools).
    This is the main entry point after activation.
    """
    cid_p = cid_prefix()
    # Lazy import to avoid potential circular import issues at startup
    try:
        from ..llm import call_grok_with_tools
    except Exception as import_err:
        logger.error(f"{cid_p}Failed to import Groksito LLM modules: {import_err}")
        await safe_reply(message, "Lo siento, estoy teniendo problemas para inicializar mi cerebro en este momento.", mention_author=False)
        return

    user_message = message.content or ""

    # Harvest vision images if relevant (now with intelligent chain support for replies + link intent)
    # Also lightweight recent images on direct mentions when referring to recent user posts/images.
    image_urls = await _harvest_vision_images(
        message, referenced, explicit_visual_reply_intent,
        is_reply_continuation=is_reply_continuation,
        has_x_link_intent=has_x_link_intent,
        is_mentioned=is_mentioned,
        user_text=user_message,
    )

    # Deeper text chain walking for referent resolution.
    # When there's a reply chain (or on mention with referent language), fetch ancestors
    # so the model sees the actual source of a YouTube link, "what the user originally posted",
    # etc. even if the direct referenced message is short or a reply-to-reply.
    # This is text-focused (links, content) and gated to stay lightweight.
    chain_contexts: list[dict] = []
    if (is_reply_continuation or is_mentioned) and (has_x_link_intent or _has_recent_referent_intent(user_message) or explicit_visual_reply_intent):
        try:
            chain_contexts = await _fetch_reply_chain_context(
                message, max_depth=3, require_images=False
            )
            if chain_contexts:
                logger.info(f"{cid_p}[ReplyChain] Fetched text chain (levels={len(chain_contexts)}) for referent / link / visual intent")
        except Exception as chain_err:
            logger.warning(f"{cid_p}[Reply] Text chain fetch failed: {chain_err}")

    # NOTE: All context injection decided inside llm_input.build_responses_input (single source of truth).
    # High-prio referenced message [R:] is injected for direct replies to the bot OR when @mentioned
    # while replying to another user (YouTube link / image / tweet the user wants us to look at).
    # No channel history / recent summary pre-injection by default (#19: recent context is on-demand tool only).
    # No custom memory (maximum nativeness / "let Grok be Grok").

    try:
        # Real call to the LLM layer (now connected to xAI Responses API + tools).
        # We pass has_image_creation_intent (strict) for media tool offering; images for vision are handled separately.
        response_text = await call_grok_with_tools(
            user_message=user_message,
            author_name=author_display,
            channel_id=message.channel.id,
            original_message=message,
            image_urls=image_urls,
            referenced_context=referenced_context,
            reply_chain_contexts=chain_contexts,  # deeper ancestors for text referents (YouTube, "what the user said", etc.)
            has_visual_intent=has_image_creation_intent,
            is_reply_continuation=is_reply_continuation,
            has_x_link_intent=has_x_link_intent,  # X/link intent signal
            is_reply_to_bot=is_reply_to_bot,
            is_mentioned=is_mentioned,
        )

        # Only send a conversational reply if the LLM layer did not already
        # perform (or trigger) a direct delivery via a media tool (image/edit/video/audio) or reply_to_user.
        # DIRECT_DELIVERY_PERFORMED is an object() sentinel ΓÇö use identity check.
        if response_text is not DIRECT_DELIVERY_PERFORMED and response_text:
            # Normalize any accidental raw Discord emoji syntax <:name:ID> back to clean :name:
            # so the emoji actually renders. The model sometimes emits the internal format.
            try:
                from . import emoji_registry
                gid = getattr(message.guild, "id", None) if getattr(message, "guild", None) else None
                response_text = emoji_registry.normalize_bot_emoji_output(response_text, gid)
            except Exception:
                pass

            await safe_reply(message, response_text, mention_author=False)
    except Exception as e:
        logger.exception(f"{cid_p}Error invoking Groksito: {e}")
        await safe_reply(message, "Ocurri├│ un error procesando tu mensaje. Intenta de nuevo.", mention_author=False)


# Backwards-compatible aliases used by client.py
_resolve_referenced_and_activation = _resolve_referenced_and_activation
_build_referenced_context = _build_referenced_context
_harvest_vision_images = _harvest_vision_images
_invoke_groksito = _invoke_groksito
