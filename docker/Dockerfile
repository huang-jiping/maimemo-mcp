# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG UV_VERSION=0.12.22
ARG MAIMEMO_VERSION=0.2.0
ARG SOURCE_REVISION=unknown

FROM ${PYTHON_IMAGE} AS build-base
ARG UV_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages

FROM build-base AS server-build
RUN uv sync --frozen --no-dev --no-editable --package maimemo-server \
    && rm -rf /root/.cache/uv

FROM build-base AS mcp-build
RUN uv sync --frozen --no-dev --no-editable --package maimemo-mcp \
    && rm -rf /root/.cache/uv

FROM build-base AS worker-build
RUN uv sync --frozen --no-dev --no-editable --package maimemo-worker \
    && rm -rf /root/.cache/uv

FROM ${PYTHON_IMAGE} AS runtime-base
ARG MAIMEMO_VERSION
ARG SOURCE_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH}
LABEL org.opencontainers.image.source="https://github.com/huang-jiping/maimemo" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${MAIMEMO_VERSION}"
RUN groupadd --system --gid 10001 maimemo \
    && useradd --system --uid 10001 --gid maimemo --home-dir /nonexistent \
        --shell /usr/sbin/nologin maimemo
WORKDIR /app
COPY --chown=root:root alembic.ini ./alembic.ini
COPY --chown=root:root migrations ./migrations
USER 10001:10001

FROM runtime-base AS server
COPY --from=server-build --chown=root:root /opt/venv /opt/venv
EXPOSE 8080
ENTRYPOINT ["maimemo-server"]

FROM runtime-base AS mcp
COPY --from=mcp-build --chown=root:root /opt/venv /opt/venv
# The installed health module resolves its pinned schema relative to the venv.
COPY --chown=root:root openapi /opt/venv/lib/openapi
COPY --chown=root:root openapi ./openapi
EXPOSE 8000
ENTRYPOINT ["maimemo-mcp"]

FROM runtime-base AS worker
COPY --from=worker-build --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root openapi ./openapi
ENTRYPOINT ["maimemo-worker"]
