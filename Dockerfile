FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY app/ app/

# Run as an unprivileged user; own /app and the data dir it writes the SQLite DB to.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p data \
    && chown -R bot:bot /app
USER bot

CMD ["uv", "run", "--frozen", "--no-dev", "python", "-m", "app.main"]
