# MCP Tool Schema Reference (not loaded at runtime)

This directory holds **reference-only** JSON tool schemas exported from various MCP (Model Context Protocol) servers. They are **not** loaded, registered, or consumed by the Groksito Discord bot at runtime.

## What is here

Eight server snapshots with 130+ tool definition files:

| Directory | Origin |
|-----------|--------|
| `grok_com_github/` | GitHub MCP tools (issues, PRs, repos, etc.) |
| `grok_com_notion/` | Notion MCP tools |
| `cloudflare-api/` | Cloudflare API MCP tools |
| `cloudflare-bindings/` | Cloudflare Workers bindings (KV, R2, D1, Hyperdrive) |
| `cloudflare-builds/` | Cloudflare Builds MCP tools |
| `cloudflare-docs/` | Cloudflare documentation search |
| `cloudflare-observability/` | Cloudflare Workers observability |
| `MCP_DOCKER/` | Browser automation and code-mode MCP tools |

Each `tools/*.json` file describes one MCP tool's name, description, and `inputSchema`.

## Why it was moved here

These files previously lived at the repository root as `mcps/`, which implied they were part of the bot's runtime. Static analysis showed **zero imports or loaders** in `src/`. The actual power tools the bot supports (`code_execution`, `playwright_browser`) are implemented explicitly in `src/groksito_discord/llm/skill_tools.py` and `sandbox.py`, gated by per-skill `allowed_tools`.

This directory is kept as **design reference** for a possible future MCP connector integration. See `docs/mcp-integration.md` for the current architecture and integration path.

## Maintenance

- **Do not** update these files expecting the bot to pick up changes automatically.
- If schemas become stale, refresh from the source MCP server export or remove the snapshot.
- These files are excluded from Docker images (see `.dockerignore`).