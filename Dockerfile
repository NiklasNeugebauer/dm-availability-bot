FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

WORKDIR /app

# Cached dependency layer: the project source isn't present yet, so install
# only the locked dependencies (not the project itself — that would build an
# empty wheel here and force a reinstall on every container start).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the source is available, install the project into the venv (non-editable).
COPY app/ app/
RUN uv sync --frozen --no-dev --no-editable

# Run as an unprivileged user; own /app and the data dir it writes the SQLite DB to.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p data \
    && chown -R bot:bot /app
USER bot

# Use the venv's python directly — no uv at runtime, no .venv mutation on start.
CMD [".venv/bin/python", "-m", "app.main"]
