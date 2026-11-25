FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS base
ENV UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# Python dependencies only (cached until pyproject.toml / uv.lock change)
FROM base AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Chromium + system libs, installed with the project's locked Playwright
# version so the browser always matches the driver. Cached with the deps.
FROM base AS runtime
COPY --from=deps /app/.venv /app/.venv
RUN apt-get update && \
    playwright install --with-deps chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Project code
FROM deps AS builder
COPY torrent_search /app/torrent_search
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM runtime
COPY --from=builder /app /app
# Browsers are baked in above: skip the install step at container start.
ENV TORRENT_SEARCH_SKIP_BROWSER_INSTALL=1

EXPOSE 8000
CMD ["torrent-search-mcp", "--mode", "http"]
