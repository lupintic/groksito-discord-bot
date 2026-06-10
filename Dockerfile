# =============================================================================
# Groksito Discord Bot — Multi-stage Production Dockerfile
# =============================================================================
# We use stages so that:
#   - `docker compose build` (or build target "bot") produces the full ~1 GB image
#     needed by the Discord bot (discord.py, openai, pydub, ffmpeg, docker-ce-cli
#     for optional skill sandboxes when the host socket is mounted, etc.).
#   - `docker compose build web` (or build target "web") produces a much smaller
#     image (~280-400 MB range) containing only the FastAPI dashboard + the tiny
#     subset of bot modules it actually imports (env_utils + skill_registry + config).
#
# This fixes the previous situation where both services used the identical heavy
# single-stage build and both images reported ~1.04 GB.
# =============================================================================

# -----------------------------------------------------------------------------
# Base stage — common slim Python + minimal system tools
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS base

WORKDIR /app

# Common minimal system packages needed by both bot and web.
# We deliberately do NOT install ffmpeg or docker-ce-cli here (bot-only).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Bot stage — full runtime for the Discord agent + power tools
# -----------------------------------------------------------------------------
FROM base AS bot

# System dependencies required only by the bot:
# - ffmpeg: audio (pydub) and video handling
# - docker-ce-cli: so that approved skills can use the code_execution /
#   playwright_browser sandboxes by running `docker run ...` against the
#   host socket (see the commented mount in docker-compose.yml).
#   The actual daemon still requires the socket to be mounted at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg \
        lsb-release \
        ffmpeg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install the full dependency set (bot + web libs)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the full application (bot + dashboard for convenience when running the bot image)
COPY src ./src
COPY web ./web

# Create runtime directories (good for volume mounts even before first write)
RUN mkdir -p /app/data && chmod 755 /app/data \
 && mkdir -p /app/oauth && chmod 755 /app/oauth

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    DATA_DIR=/app/data

# Default process for the bot service (compose can still override)
CMD ["python", "-m", "src.groksito_discord"]

# -----------------------------------------------------------------------------
# Web stage — slim dashboard-only image (FastAPI + the modules it actually uses)
# -----------------------------------------------------------------------------
FROM base AS web

# Web-only minimal dependencies.
# See requirements-web.txt for the explicit list and rationale.
COPY requirements-web.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-web.txt

# We only need the small surface that web/main.py actually imports:
#   - groksito_discord.env_utils (stdlib only)
#   - groksito_discord.skills.skill_registry (pulls config + correlation)
# Copying the whole src/ tree is simple, future-proof, and tiny (pure .py).
# The huge size saving comes from the slim pip layer + skipping the docker/ffmpeg apt work.
COPY src ./src
COPY web ./web

# Same directories as the bot image so volume mounts and path logic in web/main.py
# (BASE_DIR.parent / "src" + DATA_DIR) continue to work without surprises.
RUN mkdir -p /app/data && chmod 755 /app/data \
 && mkdir -p /app/oauth && chmod 755 /app/oauth

# Environment defaults (web service)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    DATA_DIR=/app/data

# Sensible default for the web dashboard (compose overrides with the same command,
# but this makes `docker run <web-image>` work out of the box on port 8010).
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8010"]
