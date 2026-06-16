# MCP Integration — Current State and Future Path

## Current state (what actually runs)

The Groksito Discord bot does **not** load MCP server tool schemas at runtime. Power tools are implemented directly in Python and only exposed when an approved skill declares them in `allowed_tools`.

| Tool | Implementation | Gating |
|------|----------------|--------|
| `code_execution` | `llm/skill_tools.py` schema + `llm/sandbox.py` Docker runner | Skill `allowed_tools` |
| `playwright_browser` | `llm/skill_tools.py` schema + `llm/sandbox.py` browser container | Skill `allowed_tools` |

Key modules:

- `src/groksito_discord/llm/skill_tools.py` — `get_skill_specific_custom_schemas()` injects tool schemas into the Responses API call only when the active skill permits them.
- `src/groksito_discord/llm/sandbox.py` — executes sandboxed Docker commands for the two power tools above.
- `docker-compose.yml` — optional host Docker socket mount for real sandbox execution (commented example).

## Reference schemas (not runtime)

Exported MCP tool JSON schemas live under `docs/reference/mcp-tool-schemas/`. These are snapshots from external MCP servers (GitHub, Cloudflare, browser, etc.) kept for design reference only. See that directory's README for contents and maintenance notes.

## Future integration (not implemented)

A proper MCP connector path would:

1. Let skills declare connectors in metadata (e.g. `mcp:github`, `mcp:cloudflare-bindings`).
2. Load and validate schemas from a known registry path when the skill activates.
3. Route tool calls through an MCP client with auth, timeouts, and audit logging.
4. Register new schemas in `get_skill_specific_custom_schemas()` via a loader, not hand-copied JSON.

Until that loader exists, add new power tools by extending the `known` dict in `skill_tools.py` and implementing dispatch in `sandbox.py` (or a dedicated handler), following the existing `code_execution` / `playwright_browser` pattern.

## Docker images

Runtime images copy only `src/` and `web/` (see `Dockerfile`). The `docs/` tree is excluded from the build context via `.dockerignore` and is never baked into bot or web images.