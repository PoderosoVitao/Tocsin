FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Dependencies first, in their own layer, so changing the code doesn't
# reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra server --no-install-project
COPY tocsin ./tocsin
RUN uv sync --frozen --no-dev --extra server

FROM python:3.12-slim
RUN useradd --system --uid 1000 --home-dir /app tocsin
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER tocsin
EXPOSE 8000
CMD ["tocsin", "api", "--host", "0.0.0.0"]
