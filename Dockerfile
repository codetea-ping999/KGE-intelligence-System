FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen
COPY . .
RUN uv sync --frozen
CMD ["uv", "run", "--frozen", "kge", "demo", "--db", ":memory:"]
