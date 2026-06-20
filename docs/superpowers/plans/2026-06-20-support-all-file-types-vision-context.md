# Support All File Types for Vision and Attachment Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Groksito to "see" all user-uploaded file types in addressed messages. Use native vision only for supported formats (jpg/jpeg/png). For GIFs/WebP/other images and non-images: always surface filename + type + size metadata in the prompt. For small text files (.txt, .py, .md, .json, logs etc.): inline truncated content so the model can read natively. Generalize vision error retry to prevent the canned "servicio de visión" failure message. Follow maximum nativeness, existing architecture (harvest in conversation.py, sole input builder in llm_input.py), and keep changes minimal/lightweight.

**Architecture:** 
- Extend intent.py with shared is_supported_vision_image / is_text_attachment helpers (ct primary + filename fallback for images).
- conversation.py _harvest_vision_images (or augmented) collects image_urls (filtered to supported) + full attachments metadata list. For current-msg small text attachments: fetch and inline content (capped).
- llm_input.py builds a compact [Attachments ...] block (injected into user content like [R:] refs) + keeps input_image only for supported images.
- client.py generalizes the 404-retry path to any attachment-bearing failure (retry without vision parts but with metadata always present). Remove/condition the blanket vision-canned error.
- Gated to addressed turns, preserve previous_response_id contract, stable SYSTEM_PROMPT prefix, cache friendliness.
- No new deps (httpx already present), no changes to media delivery, tools, or activation policy.

**Tech Stack:** Python 3.11+, discord.py (Attachment: url/filename/content_type/size), httpx (for safe text fetch), existing Responses API multimodal format, pytest. Follows the exact layout in ARCHITECTURE.md.

Design spec reference: `docs/superpowers/specs/2026-06-20-support-all-file-types-vision-context-design.md` (read this first).

Maximum Nativeness + rules from AGENTS.md / ARCHITECTURE.md / README (re-read before coding):
- Native vision for what Grok supports (jpg/png only).
- Provide accurate minimal context for everything else via text.
- Light, gated dynamic injection only.
- No heavy custom processing.

---

## Current State (zero-context implementer briefing)

- Vision harvesting is strict `startswith("image/")` only in:
  - `src/groksito_discord/core/conversation.py:_harvest_vision_images` (current + referenced)
  - `src/groksito_discord/discord/client.py` (for recent context tracking)
- `core/intent.py` already has good `_MEDIA_*` + `referenced_has_media_attachments` with filename fallback (for activation/media intent only).
- Attachments metadata for referenced is collected but **never turned into prompt text** (only image_urls picked).
- Current non-image attachments on the triggering message: completely invisible to LLM.
- `llm/llm_input.py:_build_multimodal_user_content` + `build_responses_input` handle image_urls → input_image.
- `llm/client.py`: special 404 retry (rebuilds input with image_urls=[]). Outer except returns the exact user-visible Spanish vision error if image_urls was truthy.
- xAI vision limitation (confirmed): only jpg/jpeg/png supported.
- User sends GIF → "image/gif" harvested → input_image → backend trouble → canned message.
- Text inlining for files: does not exist.
- Tests: `tests/test_error_observability.py` has 404 vision retry test. Media referent tests exist.
- Data flow: discord/client.py → conversation.py (harvest + invoke) → llm/client.py (call) → llm_input.py (build).

Read the design spec and ARCHITECTURE.md fully before any edit.

---

### Task 0: Preparation (read everything, baseline)

- [ ] **Step 0.1: Read the approved design spec**
  ```
  cat docs/superpowers/specs/2026-06-20-support-all-file-types-vision-context-design.md
  ```
  Expected: Understand supported formats, attachment block format, retry change, text inline cap, file responsibilities.

- [ ] **Step 0.2: Read key architecture and rules**
  ```
  cat ARCHITECTURE.md | head -100
  cat AGENTS.md
  grep -A 20 -E "Vision:|harvest|attachment|input_image" ARCHITECTURE.md
  ```

- [ ] **Step 0.3: Read current harvest and intent code**
  ```
  sed -n '321,490p' src/groksito_discord/core/conversation.py
  sed -n '480,520p' src/groksito_discord/core/intent.py
  cat src/groksito_discord/core/intent.py | grep -A 30 "_MEDIA_"
  ```

- [ ] **Step 0.4: Read llm input builder**
  ```
  sed -n '78,120p' src/groksito_discord/llm/llm_input.py
  sed -n '255,380p' src/groksito_discord/llm/llm_input.py
  ```

- [ ] **Step 0.5: Read error handling + prepare in client**
  ```
  sed -n '750,820p' src/groksito_discord/llm/client.py
  sed -n '897,935p' src/groksito_discord/llm/client.py
  sed -n '208,260p' src/groksito_discord/llm/client.py
  ```

- [ ] **Step 0.6: Baseline tests + check**
  ```
  python -m pytest tests/test_error_observability.py::test_vision_404_retry_succeeds_without_images -q --tb=line
  python -m pytest tests/test_media_referent_activation.py -q --tb=no
  python scripts/check.py --skip-docker 2>&1 | tail -5
  ```
  Expected: PASS baselines. Note any failures unrelated.

- [ ] **Step 0.7: Inspect .env.example or settings for any size hints (none for inbound text)**
  ```
  grep -i "attach\|size\|max" src/groksito_discord/config/settings.py | head -5
  ```

---

### Task 1: Add Shared Detection Helpers + Unit Tests (TDD)

**Files:**
- Modify: `src/groksito_discord/core/intent.py:485-510`
- Test: `tests/test_media_referent_activation.py` or new/append to `tests/test_error_observability.py`

- [ ] **Step 1.1: Add constants and pure helper functions at the end of intent.py (before last functions or after referenced_has_media...)**
  Append the following (exact):

  ```python
  # === Vision + attachment helpers for "see all files" (2026-06-20 feature) ===
  # Primary = content_type. Fallback to common extensions so Discord mis-reported
  # MIME types (common for GIFs, WebP from mobile) still work for metadata.
  # Only jpg/jpeg/png go to actual native vision input_image per xAI limits.
  _VISION_SUPPORTED_IMAGE_CT_PREFIXES = ("image/jpeg", "image/jpg", "image/png")
  _VISION_SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
  _TEXT_ATTACHMENT_EXTS = (
      ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".csv",
      ".log", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bat",
  )
  _TEXT_INLINE_MAX_BYTES = 64 * 1024  # 64 KiB safety cap for inlining (small files only)

  def is_supported_vision_image(att: Any) -> bool:
      """Return True only for formats we will send as input_image to xAI vision."""
      if not att:
          return False
      ct = (getattr(att, "content_type", "") or "").lower()
      if any(ct.startswith(p) for p in _VISION_SUPPORTED_IMAGE_CT_PREFIXES):
          return True
      filename = (getattr(att, "filename", "") or "").lower()
      return any(filename.endswith(ext) for ext in _VISION_SUPPORTED_IMAGE_EXTS)

  def is_text_attachment(att: Any) -> bool:
      """Small text-like files we may safely inline content for."""
      if not att:
          return False
      ct = (getattr(att, "content_type", "") or "").lower()
      if ct.startswith("text/"):
          return True
      filename = (getattr(att, "filename", "") or "").lower()
      return any(filename.endswith(ext) for ext in _TEXT_ATTACHMENT_EXTS)

  def get_attachment_meta(att: Any) -> dict[str, Any]:
      """Lightweight dict for prompt injection. Never include secrets or huge data."""
      return {
          "filename": getattr(att, "filename", "") or "unknown",
          "content_type": getattr(att, "content_type", "") or "",
          "size": getattr(att, "size", 0) or 0,
          "url": getattr(att, "url", ""),
      }
  ```

- [ ] **Step 1.2: Write failing tests first (TDD). Append to tests/test_error_observability.py or the media referent test file.**
  Use search_replace or write at end. Make sure they fail initially (new functions not exported/used yet).

  ```python
  def test_is_supported_vision_image_jpg_png_yes():
      from groksito_discord.core.intent import is_supported_vision_image
      att = type('A', (), {'content_type': 'image/jpeg', 'filename': 'x.png'})()
      assert is_supported_vision_image(att) is True

  def test_is_supported_vision_image_gif_no():
      from groksito_discord.core.intent import is_supported_vision_image
      att = type('A', (), {'content_type': 'image/gif', 'filename': 'a.gif'})()
      assert is_supported_vision_image(att) is False   # we handle via metadata only

  def test_is_text_attachment_detects():
      from groksito_discord.core.intent import is_text_attachment
      py_att = type('A', (), {'content_type': '', 'filename': 'foo.py'})()
      assert is_text_attachment(py_att) is True
      pdf_att = type('A', (), {'content_type': 'application/pdf', 'filename': 'r.pdf'})()
      assert is_text_attachment(pdf_att) is False
  ```

- [ ] **Step 1.3: Run the new tests to confirm they fail**
  ```
  python -m pytest tests/test_error_observability.py -q -k "is_supported_vision or is_text_attachment" --tb=short
  ```
  Expected: FAIL (functions may not exist yet or not imported in test scope).

- [ ] **Step 1.4: Make the helpers importable if needed (they are in same module). Re-run to pass**
  ```
  python -m pytest tests/test_error_observability.py -q -k "is_supported_vision or is_text_attachment" --tb=line
  ```
  Expected: PASS. Commit the helpers + tests.

- [ ] **Step 1.5: Commit**
  ```
  git add src/groksito_discord/core/intent.py tests/test_error_observability.py
  git commit -m "feat(core): add vision-supported + text attachment detection helpers (TDD)"
  ```

---

### Task 2: Enhance Harvesting to Collect All Attachments + Inline Small Text (TDD)

**Files:**
- Modify: `src/groksito_discord/core/conversation.py`
- Test updates.

- [ ] **Step 2.1: Read full harvest function and call sites again**
  (repeat relevant reads)

- [ ] **Step 2.2: Add import for new helpers at top of conversation.py (if not already using intent)**
  It already imports from .intent:
  ```python
  from .intent import (
      ...
      referenced_has_media_attachments,
  )
  ```
  Add the new ones: `is_supported_vision_image, is_text_attachment, get_attachment_meta, _TEXT_INLINE_MAX_BYTES`

- [ ] **Step 2.3: Write a helper inside conversation.py (or use the one in intent) for safe text fetch. Add a private async function before _harvest...**
  ```python
  async def _maybe_fetch_text_content(att: Any, cid_p: str) -> str | None:
      """Fetch small text attachment content for inlining. Returns truncated str or None."""
      try:
          size = getattr(att, "size", 0) or 0
          if size > _TEXT_INLINE_MAX_BYTES or size <= 0:
              return None
          url = getattr(att, "url", None)
          if not url:
              return None
          import httpx
          async with httpx.AsyncClient(timeout=8.0) as client:
              resp = await client.get(url)
              resp.raise_for_status()
              text = resp.text[:4000]  # hard truncate for prompt safety
              if len(text) < 10:
                  return None
              return text
      except Exception as fetch_err:
          logger.debug(f"{cid_p}[Vision] text inline fetch skipped: {fetch_err}")
          return None
  ```

- [ ] **Step 2.4: Refactor _harvest_vision_images to also return attachments. Change signature and body (minimal diff).**
  Update docstring and implementation to:
  - Always collect current + referenced attachments using get_attachment_meta.
  - For vision: only if is_supported_vision_image(att)
  - For current message only: if is_text_attachment and small, call _maybe_fetch... and add "text_content" to the meta dict.
  - Keep returning list[str] for images (back-compat via alias if needed, but update callers).
  - Actually change the function to return a tuple or augment: for now we'll make it return images and also expose attachments via a new small wrapper or modify return.

  (Exact refactored sketch — put the full working version in the edit step.)

- [ ] **Step 2.5: Add failing test first that exercises new harvesting behavior (attachments present)**
  Extend a test file. Run to see failure.

- [ ] **Step 2.6: Implement the harvest changes, update internal callers in the same file (_invoke_groksito etc.) to receive attachments list.**
  Update the call to _harvest... and pass down.

- [ ] **Step 2.7: Run relevant tests**
  ```
  python -m pytest tests/ -q -k "vision or attachment or referent" --tb=line
  ```
  Expected: new behavior works, old image paths unchanged.

- [ ] **Step 2.8: Commit**
  ```
  git add src/groksito_discord/core/conversation.py tests/...
  git commit -m "feat(core): harvest all attachments + supported vision only + small text inline"
  ```

---

### Task 3: Inject Attachment Metadata + Text into LLM Input

**Files:**
- Modify: `src/groksito_discord/llm/llm_input.py`

- [ ] **Step 3.1: Update function signatures**
  Add `attachments: list[dict] | None = None` to `build_responses_input` and `_build_multimodal_user_content` (or handle in caller).

- [ ] **Step 3.2: Implement _build_attachments_block (new private function)**
  Exact code block with example output matching the design spec.

- [ ] **Step 3.3: Modify user_content construction to prepend the attachments block when present (before or with context_note).**
  Handle both str and list cases.

- [ ] **Step 3.4: Write TDD tests that call build_responses_input with attachments and assert the block text is present + vision images still work.**
  Use monkeypatch or direct.

- [ ] **Step 3.5: Run tests**
  ```
  python -m pytest ... -k "build_responses or attachment" -q --tb=short
  ```

- [ ] **Step 3.6: Commit**

---

### Task 4: Generalize Retry and Error Path in LLM Client; Thread Attachments

**Files:**
- Modify: `src/groksito_discord/llm/client.py` (prepare, call_grok..., error handling)

- [ ] **Step 4.1: Update _prepare_first_turn_data to accept and forward attachments.**

- [ ] **Step 4.2: In the first-turn call site, capture attachments and pass to build_responses_input.**

- [ ] **Step 4.3: Generalize the if is_image_fetch_404... block to catch broader vision/attachment trouble (or wrap in broader except and always retry once with images=[] but attachments kept).**
  Preserve metadata on retry. Clear only vision urls.

- [ ] **Step 4.4: Remove or guard the old `if image_urls: return canned vision message` so model always gets a chance with metadata.**
  Update the final error messages to be more generic when attachments present.

- [ ] **Step 4.5: TDD: extend the vision 404 test + add a new test for "non-404 vision trouble on GIF should retry and succeed with metadata".**
  Mock the responses call to fail once with a processing error when images present.

- [ ] **Step 4.6: Execute the test steps (write test → run fail → impl → run pass)**

- [ ] **Step 4.7: Full relevant test run + check**
  ```
  python -m pytest tests/test_error_observability.py -q --tb=line
  python scripts/check.py --skip-docker 2>&1 | tail -10
  ```

- [ ] **Step 4.8: Commit**

---

### Task 5: Integration, Manual Verification, Polish, Final Checks

- [ ] **Step 5.1: Update any call sites in discord/client.py or elsewhere that call harvest or invoke if signatures changed (use grep first).**
  ```
  grep -n "_harvest_vision_images\|_invoke_groksito" src/groksito_discord/
  ```

- [ ] **Step 5.2: Add logging for "X attachments harvested (Y vision, Z text inlined)"**

- [ ] **Step 5.3: Verify no breakage to pure image gen or non-addressed paths**

- [ ] **Step 5.4: Run full verification command**
  ```
  python -m pytest tests/ -q --tb=no
  python scripts/check.py --skip-docker
  ```

- [ ] **Step 5.5: Manual simulation (if bot runnable) or unit-level attachment roundtrip test.**
  Create a small one-off test script if needed.

- [ ] **Step 5.6: Update the design spec status if desired (optional).**

- [ ] **Step 5.7: Final commit**
  ```
  git commit -am "feat(llm+core): full support for seeing all attachment types (GIFs, text, pdfs via metadata)"
  ```

---

## Self-Review Against Spec (perform after plan written)

- [ ] Skim spec sections 3.1-3.5: every requirement maps to at least one Task/Step above.
- [ ] No "TBD", no vague "add error handling".
- [ ] Exact paths + line ranges where possible.
- [ ] Tests written before impl (TDD).
- [ ] Attachments always flow even on vision retry.
- [ ] Only jpg/png to vision; everything else metadata.
- [ ] Text inline capped and only for current addressed messages.
- [ ] Matches "Current State" briefing and zero-context assumption.

**Plan complete and saved to `docs/superpowers/plans/2026-06-20-support-all-file-types-vision-context.md`.**

Two execution options:
1. **Subagent-Driven (recommended)** — dispatch fresh subagents per task using subagent-driven-development skill, review between tasks.
2. **Inline Execution** — use executing-plans skill to run the checklist in this session with checkpoints.

Which approach do you want? (Reply with 1 or 2, or "start with subagent" etc.) Once chosen we can begin safe implementation with frequent verification.