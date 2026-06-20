# Support All File Types for Vision and Attachment Context Design

**Date**: 2026-06-20  
**Status**: Design presented in chat + user explicitly approved ("yes continue i approve"). Written and committed per superpowers/brainstorming + systematic-debugging flow.  
**Related**: User-reported error "Tuve problemas procesando la(s) imagen(es)... El servicio de visión de Grok está teniendo dificultades con este archivo..."; suspected GIFs; request to "make him be able to see all type of files" following ARCHITECTURE.md + README + AGENTS.md.

## 1. Problem Statement

Groksito cannot process ("see") certain user-uploaded files and surfaces a hard-coded vision failure message instead of graceful handling or useful context.

**Exact symptom**:
```
Tuve problemas procesando la(s) imagen(es) que enviaste. El servicio de visión de Grok está teniendo dificultades con este archivo en este momento. Por favor, describe lo que ves en la imagen con palabras y te ayudo con eso.
```

**Root causes** (from systematic debugging Phase 1 — error messages read, data flow traced, code comparison, recent commits reviewed):

1. **Narrow vision harvesting** (`core/conversation.py:354`, `376`; `discord/client.py:934`):
   - Only `content_type.startswith("image/")` (or loose `"image" in ct`).
   - **No filename extension fallback** (contrast with `core/intent.py:488-508` which has `_MEDIA_FILENAME_EXTENSIONS` fallback for video detection).
   - Text-extracted URLs have limited ext logic, but direct attachments do not.

2. **Unsupported formats reach the vision path**:
   - xAI Responses API vision (per official docs) officially supports **only jpg/jpeg and png** (max ~20 MiB). GIF (`image/gif`), WebP (`image/webp`), HEIC, animated, etc. are not supported.
   - Discord frequently delivers WebP/GIF from mobile/web. These get turned into `input_image` blocks anyway.
   - Result: backend processing error → blanket fallback.

3. **Non-image files are invisible**:
   - Current-message attachments that are not images: **zero metadata** passed to the model.
   - Referenced attachments collect full `{url, filename, content_type}` in `_build_referenced_context`, but **only `image_urls` are extracted**; the attachment list is never serialized into the prompt text (only `[R:]` content + image_urls for vision).
   - Model has no idea a PDF, .py, .log, video, etc. was attached.

4. **Brittle error handling** (`llm/client.py:897-930`, 760+):
   - Any exception on first-turn Responses call + `image_urls` present → returns the canned Spanish vision message.
   - Retry-without-images logic exists **only** for `is_image_fetch_404_error` (stale URLs).
   - Other failures (format rejection, processing trouble, 4xx/5xx that aren't pure 404, etc.) short-circuit and never give the model a chance to respond using available context.
   - The canned message is returned at the orchestrator level before any model output or tool loop.

5. **Multimodal construction** (`llm/llm_input.py:78-112`):
   - `_build_multimodal_user_content` blindly trusts harvested URLs for `input_image`.
   - No attachment metadata block is ever built for the current message.
   - Context injection is excellent for refs/emotes but incomplete for attachments.

This violates **Maximum Nativeness** (ARCHITECTURE.md, README, AGENTS.md):
- "Native vision: processes images from attachments..."
- Trust Grok's capabilities + provide accurate minimal context.
- Lightweight, cache-friendly gated dynamic info.
- "No automatic long-term memory injection or heavy context stuffing."

User clarification (via ask_user_question):
- Suspects GIFs.
- Wants **full support**: inline content for small text-based files (so Grok literally reads code/logs) + metadata for *all* attachments (PDFs etc. by name/type) + robust handling for images (incl. GIFs) + retry on trouble.

## 2. Requirements & Constraints

- **Make Groksito able to "see" all file types**:
  - Supported images (jpg/jpeg/png by ct or ext): native vision (`input_image`).
  - GIFs, WebP, other images: attachment metadata (graceful; model can ask user to describe frames).
  - Text-like files (text/* + .txt .md .py .json .csv .log .js etc.): metadata + inline small content snippet so Grok reads natively via text.
  - Everything else (PDF, video, binary, docx, etc.): metadata only (name, type, size) + model can advise "screenshot key pages".
- **Error resilience**: Never hard-fail to the canned vision-blame message when attachments are present. Generalize retry-without-vision path. Model should receive metadata and respond naturally.
- **Follow architecture strictly** (AGENTS.md, ARCHITECTURE.md):
  - Harvest logic lives in `core/conversation.py`.
  - Single source of truth for input: `llm/llm_input.py` (exactly one system + user message; dynamic notes folded into user turn for cache efficiency).
  - Gated to addressed turns.
  - Maximum nativeness: use Grok vision where supported; use text for documents. No new heavy processing libs.
  - Preserve `previous_response_id` contract (vision only on first turn).
  - Sentinel / direct delivery / media_tools untouched.
  - Lightweight (small caps on inlines, age gates, etc.).
- **Safety & ops**:
  - Size cap on text inlining (e.g. 64-128 KB; use `att.size` + fetch limit).
  - Only fetch on activated turns.
  - Errors during fetch → degrade gracefully to metadata only.
  - No secrets, no web dashboard changes.
- **Testing**: Expand existing vision/attachment tests. Add coverage for metadata injection and text inline.
- **No scope creep**: No PDF rasterization, no OCR, no video frame extraction, no change to outgoing media, no MCP/skill reintroduction.
- **Docs**: Update this spec only; ARCHITECTURE.md only if structure materially changes.

Approved approach: **Approach 2 (full native support)** from the chat presentation.

## 3. Proposed Design

### 3.1 Shared Detection Logic (core/intent.py)
Centralize and extend existing patterns:

```python
# New / extended near _MEDIA_* constants
_VISION_SUPPORTED_IMAGE_CT = ("image/jpeg", "image/jpg", "image/png")
_VISION_SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
_TEXT_ATTACHMENT_EXTS = (".txt", ".md", ".py", ".json", ".csv", ".log", ".js", ".ts", ".yml", ".yaml", ".toml", ".ini", ".cfg")

def is_supported_vision_image(att: Any) -> bool:
    ct = (getattr(att, "content_type", "") or "").lower()
    if any(ct.startswith(p) for p in _VISION_SUPPORTED_IMAGE_CT):
        return True
    fn = (getattr(att, "filename", "") or "").lower()
    return any(fn.endswith(e) for e in _VISION_SUPPORTED_IMAGE_EXTS)

def is_text_attachment(att: Any) -> bool:
    ct = (getattr(att, "content_type", "") or "").lower()
    if ct.startswith("text/"):
        return True
    fn = (getattr(att, "filename", "") or "").lower()
    return any(fn.endswith(e) for e in _TEXT_ATTACHMENT_EXTS)

def get_attachment_meta(att: Any) -> dict:
    return {
        "filename": getattr(att, "filename", ""),
        "content_type": getattr(att, "content_type", ""),
        "size": getattr(att, "size", 0),
        "url": getattr(att, "url", ""),
    }
```

`referenced_has_media_attachments` remains unchanged (it is for activation + I2V intent, not vision filtering).

### 3.2 Harvesting (core/conversation.py)
- Rename/extend `_harvest_vision_images` (or add sibling) to also collect attachments for the *current* message on every addressed turn.
- Current message attachments (always harvested when we reach `_invoke_groksito`):
  - Vision images: only those where `is_supported_vision_image(att)` → add to `image_urls`.
  - All attachments: collect meta list.
  - Text attachments (small): if `att.size` small, fetch content (prefer `att.read()` if available in discord.py, else httpx GET with `Content-Length` guard + timeout), truncate (e.g. 4000 chars), store in meta as `text_content`.
- Referenced message: enrich existing attachment collection with the same meta shape.
- Keep all existing guards: age for recent, `filter_unreliable_vision_urls` (only on vision urls), caps (max 5 vision), logs.
- Return both `image_urls` and `attachments: list[dict]`.
- Pass `attachments` through `_invoke_groksito` → `call_grok_for_groksito`.

Fetch helper (new small util or inline, async, defensive):
- Only on current message (user just sent it → fresh URL).
- Size guard before fetch.
- `httpx.AsyncClient(timeout=short)` or discord attachment methods.
- On any error: keep meta, omit text_content.

### 3.3 Prompt Construction (llm/llm_input.py)
- `build_responses_input` gains optional `attachments: list[dict] | None = None`.
- New private `_build_attachments_block(attachments) -> str`:
  - Compact, language-neutral-ish header.
  - For each: `- {filename} ({content_type}{size})`
  - If `text_content`: append fenced block with truncation note.
  - For vision images that were also harvested: still list in block (for filename awareness) + separate `input_image`.
- Inject the block **early** into the user content (same pattern as dynamic referenced context + emoji header):
  - Prepend to plain text.
  - For multimodal: insert as first `input_text` or merge.
- Pure image/video gen paths stay minimal (skip or very light note).
- `filter_unreliable...` still applied only to vision urls.

Example injected text:
```
[Attachments sent with this message:
- funny.gif (image/gif, 2.3MB)
- main.py (text/x-python, 4.1KB)
  ```python
  def foo(): ...
  ```
- design.pdf (application/pdf, 1.4MB)
]
```

This gives the model full awareness without violating cache prefix stability.

### 3.4 Orchestration & Error Handling (llm/client.py)
- In the first-turn try/except around `_call_responses_with_retry`:
  - On **any** exception when original attachments or image_urls were non-empty:
    - Log the trouble (include attachment count + first filenames).
    - Rebuild input via `build_responses_input(..., image_urls=[], attachments=the_original_attachments)` (metadata always present).
    - Retry the call once.
- On second failure: fall through to existing general error classification (rate, timeout, 5xx, auth) — **do not** take the old `if image_urls: return canned_vision_message` path.
- Remove or heavily condition the hardcoded vision message at 902-906. It is only a last resort and should mention filenames when known.
- Update `_prepare_first_turn_data` and call sites to thread attachments.
- Vision-only on first turn remains (continuations get the metadata via previous_response_id + any text notes).

Result: GIFs trigger "trouble" log + retry with metadata; model sees the GIF name and can respond helpfully ("I see you attached funny.gif but vision only supports static JPG/PNG...").

### 3.5 Other Touch Points (minimal)
- `llm/llm_utils.py` (if needed for stub or logging): pass-through or minor.
- Token / context logging: treat attachments presence similarly to `has_images`.
- No changes to:
  - `llm/tools.py`, `media/*`, `delivery.py`, `discord/client.py` (except possibly more precise logging of attachments).
  - `context/`, prompt_builder (SYSTEM_PROMPT already says use vision natively).
  - Web dashboard, config, OAuth, tests unrelated to attachments/vision.
- If size/config needed later: add small `text_attachment_inline_max_bytes` to settings (default conservative).

### 3.6 Testing Strategy
- Expand `tests/test_error_observability.py`: test non-404 vision failure path → retry succeeds with metadata; model receives attachment notes.
- `tests/test_media_referent_activation.py` + new attachment-focused test: current-msg non-image + text files + GIFs produce correct meta + (for text) content.
- `test_response_quality.py` or dedicated: snapshot-style checks that attachment blocks appear correctly in built input.
- Manual + pytest: send mixed attachments (jpg + gif + .py + .pdf) on @mention; assert no canned message, model references content or filename appropriately.
- Run full `python scripts/check.py --skip-docker` (or equivalent) before claiming done.
- Use `verification-before-completion` skill.

## 4. Trade-offs & Why This Design
- **Full vs minimal**: User chose full (text inlining). Inlining is cheap for small files and gives real value ("Grok can literally read the code I just pasted").
- **Inline vs tool**: No custom tool for "read_attachment" — that would be heavier and less native than simply putting the text in the prompt (Grok already excels at code).
- **Fetch cost**: Gated, capped, only text files, only on addressed turns. Acceptable.
- **Cache impact**: Attachment block is dynamic (like [R:] and emoji header) and folded into the user message — preserves the stable SYSTEM_PROMPT prefix.
- **Supported images only for vision**: Prevents the exact failure mode. Metadata ensures nothing is "lost".
- **Fits nativeness**: Uses Grok vision for what it supports; uses text context for documents. No extra model calls for vision on unsupported.

## 5. Files Changed (scoped)
- `src/groksito_discord/core/intent.py`
- `src/groksito_discord/core/conversation.py`
- `src/groksito_discord/llm/llm_input.py`
- `src/groksito_discord/llm/client.py`
- Tests (new/expanded)
- This design doc

No other files.

## 6. Next Steps (per superpowers)
1. User reviews this written spec file.
2. On approval: invoke `writing-plans` skill to produce implementation plan (then use `execute-plan` or direct work with verification).
3. TDD where practical (failing test for the error path first).
4. `verification-before-completion` + full check before any PR/ticket finish.

## Self-Review (done before writing this file)
- No placeholders ("TBD").
- Consistent with presented chat design + user choice.
- Internal consistency: harvest → input → orchestration.
- Scope is single focused change.
- Ambiguity removed: exact supported formats, caps, injection location, error path all specified.
- Matches AGENTS.md (respect architecture, only touch listed places, write tests).

Spec written and will be committed. User: please review the file at the path below and confirm (or request changes) before we move to writing the implementation plan.
