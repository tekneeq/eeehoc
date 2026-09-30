# NHL Live chiclets dashboard (same container pattern as eeefut / eeesoc).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src ./src
COPY scripts ./scripts
COPY README.md ./
RUN uv sync --frozen \
    && chmod +x scripts/docker-entrypoint.sh

EXPOSE 8083

ARG GIT_SHA=unknown
ARG GIT_COMMIT_TIME=unknown
ENV EEEHOC_GIT_SHA=$GIT_SHA \
    EEEHOC_GIT_COMMIT_TIME=$GIT_COMMIT_TIME \
    EEEHOC_HOST=0.0.0.0 \
    EEEHOC_PORT=8083 \
    UV_NATIVE_TLS=true \
    PYTHONUNBUFFERED=1

CMD ["./scripts/docker-entrypoint.sh"]
