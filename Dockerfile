# Standalone Teamscale REST API MCP server, run as a sidecar next to Teamscale.
#
# Multi-stage: resolve dependencies into a venv with uv in the builder, then
# ship only the venv + app on a plain python-slim runtime. This keeps the ~45MB
# uv/uvx binaries out of the final image.

# ---- builder: create /app/.venv using uv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Use the image's system CPython (never download a managed one), so the venv
# references /usr/local/bin/python3.12 -- which also exists in the runtime image.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-dev

# ---- runtime: slim python with just the venv + app (no uv) ----
FROM python:3.12-slim-bookworm

# curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/server.py ./

# Put the venv on PATH so `python` is the venv interpreter.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8081

# curl is installed above for exactly this. urlopen-style probes are not
# needed: /health is a plain 200.
# `|| exit 1` normalises curl's exit codes — Docker reserves exit 2, which
# curl uses for its own init failures.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -sf -o /dev/null http://localhost:8081/health || exit 1

# All configuration is via environment variables (see README).
CMD ["python", "server.py"]
