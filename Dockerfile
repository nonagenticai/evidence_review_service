# -----------------------------
# Stage 0: Base (shared)
# -----------------------------
ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS base

ENV UV_SYSTEM_PYTHON=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:$PATH"

WORKDIR /app

# Multi-arch compatible uv binary
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /uvx /usr/local/bin/


# -----------------------------
# Stage 1: Builder
# -----------------------------
FROM base AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Prime cache with metadata + lock
COPY README.md pyproject.toml uv.lock* ./

# Install dependencies only (cached layer — not invalidated by code changes).
# --frozen: the committed uv.lock is the resolution. No `uv lock` here; a
# re-resolve inside the image would let the built artifact differ from the one
# CI audited.
RUN uv sync --frozen --no-dev --no-install-project \

# App source after deps for better cache reuse
COPY src/ /app/src/

# Install the local project (fast — deps already cached)
RUN uv sync --frozen --no-dev \

# The seal must be usable in the IMAGE, not merely in the source tree.
# `research_contracts` force-includes its released JSON Schemas into the wheel
# because canonical/registry.py resolves every $id from them. That wheel once
# shipped ZERO schema files and `import research_contracts.canonical` raised on a
# fresh install while 446 checks stayed green -- Path.glob over a missing
# directory returns [] silently. Fail the BUILD here rather than discover it when
# an export cannot be sealed in production.
RUN /app/.venv/bin/python -c "\
from research_contracts.canonical import compute_content_hash, verify_content_hash; \
from research_contracts.canonical.registry import canonical_schemas; \
n = len(canonical_schemas()); print(f'research_contracts: {n} released schemas'); \
assert n > 0, 'the installed wheel carries no schemas; the export seal cannot work'"


# -----------------------------
# Stage 2: Production
# -----------------------------
FROM base AS production

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root execution. Created before the app-content copies and chowned via
# --chown= on each COPY instead of a trailing `chown -R`: a cache-cold build
# (ARC runners start with empty dind) paid a recursive chown over the whole
# .venv twice -- inode traversal plus a second non-dedupable layer -- measured
# at 596s of this repo's build time.
RUN useradd -m -u 1000 appuser && chown appuser:appuser /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml
COPY --from=builder --chown=appuser:appuser /app/uv.lock /app/uv.lock
COPY --from=builder --chown=appuser:appuser /app/README.md /app/README.md
COPY --chown=appuser:appuser src/ /app/src/
COPY --chown=appuser:appuser scripts/ /app/scripts/
COPY --chown=appuser:appuser alembic.ini /app/alembic.ini
COPY --chown=appuser:appuser alembic /app/alembic

USER appuser

EXPOSE 8810
ENV PORT=8810

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8810", "--workers", "1"]
