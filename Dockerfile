# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ARG UV_VERSION=0.12.21

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:${PATH}

RUN groupadd --system --gid 10001 maimemo \
    && useradd --system --uid 10001 --gid maimemo --home-dir /nonexistent \
        --shell /usr/sbin/nologin maimemo \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && rm -rf /root/.cache/uv

COPY src ./src
COPY openapi ./openapi
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

RUN uv sync --frozen --no-dev \
    && rm -rf /root/.cache/uv \
    && chown -R root:root /app /opt/venv \
    && chmod -R a-w /app /opt/venv

USER 10001:10001
EXPOSE 8000

ENTRYPOINT ["/opt/venv/bin/python", "-m", "maimemo_mcp.runtime"]
CMD ["mcp"]
