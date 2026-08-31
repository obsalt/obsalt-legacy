# obsalt core + every first-party provider plugin, from the uv workspace.
# `docker compose up -d` builds this and runs serve + worker.
# Optional groundedness extra is not in this image (`uv sync --extra groundedness`).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-packages --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm

RUN groupadd --system obsalt && useradd --system --gid obsalt obsalt

WORKDIR /app
COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER obsalt
EXPOSE 8080

CMD ["obsalt", "serve"]
