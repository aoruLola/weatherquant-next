# syntax=docker/dockerfile:1.7
#
# WeatherQuant Next collector image.
#
# Choices worth knowing before you change them:
#
#   * Multi-stage with a separate `uv` image, so the toolchain that resolves
#     dependencies never lands in the runtime layer.
#   * `UV_LINK_MODE=copy` because the build cache and the target are often on
#     different filesystems inside BuildKit, and hardlinking then fails.
#   * `--no-editable` so the package is installed as a real wheel into the
#     virtualenv. That makes the venv copyable: the runtime stage contains the
#     installed code, not a symlink back into a build directory it never sees.
#   * Bytecode compiled in the builder. The container has no compiler and
#     compiling at first start would slow every cold boot.
#   * Non-root from the runtime stage on. The data volume is written for weeks
#     at a time; running as root would make the first chown on a host-mounted
#     appdata directory a root-owned mess.
#   * No EXPOSE. The collector makes outbound calls only.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layers first: a source change must not re-resolve the lockfile.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the package itself, installed non-editable so /app/.venv is self-contained.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && /app/.venv/bin/python -c "import weatherquant; print('weatherquant', weatherquant.__version__)"

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA}
ENV GIT_SHA=${GIT_SHA} IMAGE_TAG=sha-${GIT_SHA}

RUN groupadd --gid 1000 wq \
    && useradd --uid 1000 --gid wq --no-create-home --home-dir /app --shell /usr/sbin/nologin wq

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    WQ_DB_PATH=/data/weatherquant.duckdb \
    WQ_RAW_DIR=/data/raw

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=wq:wq docker/healthcheck.py /usr/local/bin/healthcheck.py

# The data directory is a bind mount from the host; created here so a fresh
# container without a pre-made host directory still starts as the non-root user.
RUN mkdir -p /data && chown wq:wq /data

USER wq
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

# No shell form: PID 1 is the collector, so SIGTERM reaches it directly and the
# graceful-shutdown path runs instead of being swallowed by an intermediary.
ENTRYPOINT ["weatherquant-collect"]
