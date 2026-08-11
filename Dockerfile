# Development and test harness only -- this image is never published and is not the
# deliverable. The deliverable is the wheel/sdist built from this same tree. The image
# exists so that linting, type checking and the test suite run identically on a laptop
# and in CI without anything being installed on the host.
#
# Pinned to the image INDEX (manifest-list) digest rather than a per-arch child, so the
# correct binary is selected per build platform. uv 0.11.25 (2026-06-27, within the
# 45-day soak); digest verified against ghcr.io.
FROM ghcr.io/astral-sh/uv:0.11.25-python3.14-trixie-slim@sha256:2b2e474b3a72e84c92b18a2f011a14adcb045fb361f7d8667ed1f8f55eefdafd

# The virtualenv lives outside /app so that bind-mounting the working tree over /app
# during development does not shadow it. Without this, `docker compose run` would mask
# the environment built at image-build time and every command would re-create it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as a non-root user. Two reasons, and the second is the one felt daily:
#   * a process that does not need root should not have it, even in a test harness;
#   * the working tree is bind-mounted, so a root container writes root-owned files
#     into it -- coverage data, build output, tool caches that then need sudo to clean.
# UID 1000 matches the first non-system user on a typical Linux host, so files created
# in the bind mount are owned by the developer rather than by nobody.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /opt/venv /opt/uv-cache /app \
    && chown -R appuser:appuser /opt/venv /opt/uv-cache /app

WORKDIR /app
USER appuser

# Dependencies first, as their own layer: they change only when uv.lock changes, so
# editing source does not re-resolve or re-download anything.
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --group dev

# Then the project itself. uv installs the root project in editable mode, so the
# bind-mounted source in docker-compose.yml is what actually runs.
COPY --chown=appuser:appuser . .
RUN uv sync --frozen --group dev

CMD ["uv", "run", "--frozen", "pytest"]
