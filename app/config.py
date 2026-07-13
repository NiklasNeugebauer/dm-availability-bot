import os

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# dm API endpoints (reverse-engineered from www.dm.de, may change without notice)
DM_AVAILABILITY_URL = "https://products.dm.de/availability/api/v1/tiles/DE/{dans}"
DM_PRODUCT_SEARCH_URL = "https://product-search.services.dmtech.com/de/search"
DM_STORE_BBOX_URL = "https://store-data-service.services.dmtech.com/stores/bbox/{nw_lat},{nw_lon},{se_lat},{se_lon}"
DM_STORE_ITEM_URL = "https://store-data-service.services.dmtech.com/stores/item/{store_id}"

# Geocoding (PLZ/city -> coordinates) via OpenStreetMap Nominatim
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "dm-availability-bot/0.1 (personal availability watcher)"

# Polling
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "15"))

# Rate limiting: minimum seconds between requests to dm services
DM_MIN_REQUEST_INTERVAL = float(os.environ.get("DM_MIN_REQUEST_INTERVAL", "2"))
DM_REQUEST_TIMEOUT = 30

# How many DANs to query per availability request
AVAILABILITY_BATCH_SIZE = 25

# Store search radius around the geocoded point (km)
STORE_SEARCH_RADIUS_KM = float(os.environ.get("STORE_SEARCH_RADIUS_KM", "10"))
MAX_STORE_RESULTS = 6

# Product search
SEARCH_PAGE_SIZE = 8

# Persistence
DB_PATH = os.environ.get("DB_PATH", "data/bot.db")

# Abuse protection
# Max products a single chat may watch (guards DB growth and per-chat poll cost).
MAX_SUBSCRIPTIONS_PER_CHAT = int(os.environ.get("MAX_SUBSCRIPTIONS_PER_CHAT", "15"))
# dm article numbers are far below this; the bound also guards against SQLite
# 64-bit INTEGER overflow from a forged/oversized value.
MAX_DAN = 10**10
# In-memory product-name cache (dan -> name), bounded to avoid unbounded growth.
MAX_TITLE_CACHE = 1000
# Optional allowlist of Telegram chat IDs (comma-separated). Empty = open to everyone.
_allowed = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = (
    frozenset(int(c) for c in _allowed.split(",") if c.strip()) if _allowed else frozenset()
)
