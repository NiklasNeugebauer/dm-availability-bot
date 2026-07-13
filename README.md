# dm availability bot

Telegram bot that watches product availability at your local
**dm-drogerie markt** store and notifies you when a product comes back in
stock or sells out.

## How it works

Talk to the bot:

| Command | Description |
|---------|-------------|
| `/store <PLZ or city>` | Choose your dm store (or just share a location) |
| `/search <query>` | Search dm products, subscribe with one tap |
| `/subscribe <DAN>` | Watch a product by its dm article number |
| `/unsubscribe <DAN>` | Stop watching a product |
| `/list` | Your subscriptions with current status |
| `/check` | Check availability right now |
| `/help` | Command overview |

A background job polls the dm availability API every `CHECK_INTERVAL_MINUTES`
(default 30) and sends a message whenever a watched product flips between
available and unavailable at your store. The first check after subscribing
only records the current state — you are notified on *changes*.

## Data sources

The bot talks to dm's public but **undocumented** web services (the same ones
www.dm.de uses). They may change without notice — they have before.

| Service | Endpoint |
|---------|----------|
| Product search | `https://product-search.services.dmtech.com/de/search?query=…` |
| Store availability | `https://products.dm.de/availability/api/v1/tiles/DE/{dans}?pickupStoreId={storeId}` |
| Store finder | `https://store-data-service.services.dmtech.com/stores/bbox/{nw},{se}` |
| Store details | `https://store-data-service.services.dmtech.com/stores/item/{storeId}` |
| Geocoding (PLZ/city) | `https://nominatim.openstreetmap.org/search` |

### dm server protection

- Minimum 2 seconds between consecutive requests to dm services
  (configurable via `DM_MIN_REQUEST_INTERVAL`)
- One availability request per store per poll cycle, DANs batched
  (25 per request), deduplicated across subscribers
- The poll cycle makes no requests when there are no subscriptions

Please keep the polling interval reasonable — this is meant as a personal
convenience bot, not a scraping farm.

## Tech stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.11+ |
| Bot framework | python-telegram-bot (async, JobQueue) |
| HTTP client | httpx (async) |
| Persistence | SQLite (stdlib) |
| Packaging | uv + hatchling |
| Container | Docker |

## Project structure

```
app/
  config.py      Environment-based configuration, service URLs
  dm_api.py      dm API client: search, stores, availability parsing, geocoding
  storage.py     SQLite: chats (chosen store) + subscriptions + last state
  bot.py         Telegram handlers + periodic availability check
  main.py        Entrypoint: application setup, handler registration, job queue
tests/
  conftest.py    Temp-DB fixture, synthetic availability tiles
  test_dm_api.py
  test_storage.py
  test_bot.py
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Provide the token as `TELEGRAM_BOT_TOKEN`.

### Local development

```sh
export TELEGRAM_BOT_TOKEN=123456:ABC...
uv sync --group dev
uv run python -m app.main
```

### Running tests

```sh
uv run pytest tests/ -v
```

Tests cover tile parsing, geo helpers, rate limiting, persistence, the
handlers, and the notification logic. They run fully offline (mocked API).

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Bot token from @BotFather |
| `CHECK_INTERVAL_MINUTES` | `30` | How often to poll availability |
| `DM_MIN_REQUEST_INTERVAL` | `2` | Minimum seconds between dm requests |
| `DM_REQUEST_TIMEOUT` | `30` | Per-request timeout in seconds |
| `STORE_SEARCH_RADIUS_KM` | `10` | Store search radius around a location |
| `DB_PATH` | `data/bot.db` | SQLite database location |
| `MAX_SUBSCRIPTIONS_PER_CHAT` | `15` | Max products a single chat may watch |
| `ALLOWED_CHAT_IDS` | — | Optional comma-separated chat-ID allowlist (empty = open to everyone) |

## Deployment

```sh
echo "TELEGRAM_BOT_TOKEN=123456:ABC..." > .env
docker compose up -d --build
docker compose logs -f dm-bot
```

The `bot_data` volume persists the SQLite database (your store choice and
subscriptions) across rebuilds. The bot uses long polling — no inbound
ports, reverse proxy, or public hostname needed.

## Disclaimer

This project is not affiliated with dm-drogerie markt. It uses the same
web APIs the dm website calls, for personal use. Availability data
(including stock counts) is whatever dm reports — treat it as an estimate.
