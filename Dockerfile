# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Facebook Pages MCP Server - production image
#
# Build:   docker build -t facebook-pages-mcp-server .
# Run:     docker run --rm -p 8900:8900 --env-file .env facebook-pages-mcp-server
# Compose: docker compose up -d --build
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install dependencies first (layer cache), then the code.
COPY pyproject.toml README.md ./
COPY facebook_pages_mcp_server ./facebook_pages_mcp_server

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim

# Non-root user (the server only needs outbound network access).
RUN groupadd --gid 10001 fbpages && \
    useradd --uid 10001 --gid fbpages --create-home --shell /usr/sbin/nologin fbpages

COPY --from=builder --chown=fbpages:fbpages /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_HTTP_HOST=0.0.0.0

# MCP endpoint (streamable HTTP) and unauthenticated /health probe.
EXPOSE 8900

USER fbpages

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/health', timeout=4)" || exit 1

# Default: serve streamable HTTP (requires MCP_API_KEY in the environment).
# For stdio (local MCP clients), run: docker run --rm -i --env-file .env facebook-pages-mcp-server
CMD ["facebook-pages-mcp-server", "serve-http"]
