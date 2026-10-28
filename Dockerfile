# Stage 1: build the React app
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app/ app/
COPY data/seed/ data/seed/
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist web/dist
EXPOSE 8000
CMD ["sh", "-c", "exec /srv/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
