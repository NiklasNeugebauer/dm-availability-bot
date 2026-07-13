"""Client for dm's (undocumented) product, store, and availability services.

Endpoints are reverse-engineered from www.dm.de and may change without
notice. All requests are rate-limited to be a polite consumer.
"""

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass

import httpx

from app.config import (
    AVAILABILITY_BATCH_SIZE,
    DM_AVAILABILITY_URL,
    DM_MIN_REQUEST_INTERVAL,
    DM_PRODUCT_SEARCH_URL,
    DM_REQUEST_TIMEOUT,
    DM_STORE_BBOX_URL,
    DM_STORE_ITEM_URL,
    MAX_STORE_RESULTS,
    NOMINATIM_URL,
    SEARCH_PAGE_SIZE,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

STOCK_RE = re.compile(r"\((\d+)\)")


@dataclass
class Product:
    dan: int
    brand: str
    title: str

    @property
    def name(self) -> str:
        return f"{self.brand} {self.title}".strip()


@dataclass
class Store:
    store_id: str
    street: str
    zip: str
    city: str
    lat: float
    lon: float
    distance_km: float = 0.0

    @property
    def name(self) -> str:
        return f"{self.street}, {self.zip} {self.city}"


@dataclass
class Availability:
    dan: int
    store_available: bool | None  # None = no store information in response
    store_stock: int | None
    online_available: bool | None


def parse_tile(dan: int, tile: dict) -> Availability:
    """Extract store/online availability from one availability-tile entry.

    A tile contains display rows like
      {"icon": "GREEN", "text": "Lieferbar"}                           (online shop)
      {"icon": "GREEN", "text": "<linking>Dein dm-Markt</linking> (11)"}  (chosen store)
    """
    store_available = None
    store_stock = None
    online_available = None
    for row in tile.get("rows", []):
        text = row.get("text", "") or ""
        icon = row.get("icon", "")
        if "dm-Markt" in text:
            match = STOCK_RE.search(text)
            store_stock = int(match.group(1)) if match else None
            if store_stock is not None:
                store_available = store_stock > 0
            else:
                store_available = icon == "GREEN"
        elif "lieferbar" in text.lower():
            online_available = icon == "GREEN"
    return Availability(dan, store_available, store_stock, online_available)


def bbox_around(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """North-west / south-east bounding box corners around a point."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat + dlat, lon - dlon, lat - dlat, lon + dlon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_store(data: dict) -> Store | None:
    """Parse one store entry; returns None if the payload is malformed."""
    store_id = data.get("storeId")
    if not store_id:
        return None
    address = data.get("address", {}) or {}
    location = data.get("location", {}) or {}
    return Store(
        store_id=str(store_id),
        street=address.get("street", "") or "",
        zip=address.get("zip", "") or "",
        city=address.get("city", "") or "",
        lat=_safe_float(location.get("lat")),
        lon=_safe_float(location.get("lon")),
    )


class DmApi:
    """Rate-limited async client for dm services."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=DM_REQUEST_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get_json(self, url: str, params: dict | None = None):
        async with self._lock:
            wait = DM_MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        response = await self._get_client().get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def search_products(self, query: str, page_size: int = SEARCH_PAGE_SIZE) -> list[Product]:
        data = await self._get_json(DM_PRODUCT_SEARCH_URL, params={"query": query, "pageSize": page_size})
        products = []
        for item in data.get("products", []):
            if "dan" not in item:
                continue
            try:
                dan = int(item["dan"])
            except (TypeError, ValueError):
                continue  # skip a malformed entry rather than failing the whole search
            products.append(
                Product(
                    dan=dan,
                    brand=item.get("brandName", ""),
                    title=item.get("title", ""),
                )
            )
        return products

    async def get_availability(self, store_id: str, dans: list[int]) -> dict[int, Availability]:
        """Availability of the given products at one store (batched requests)."""
        result: dict[int, Availability] = {}
        for i in range(0, len(dans), AVAILABILITY_BATCH_SIZE):
            batch = dans[i : i + AVAILABILITY_BATCH_SIZE]
            url = DM_AVAILABILITY_URL.format(dans=",".join(str(d) for d in batch))
            data = await self._get_json(url, params={"pickupStoreId": store_id})
            for dan in batch:
                tile = data.get(str(dan))
                if tile is not None:
                    result[dan] = parse_tile(dan, tile)
        return result

    async def find_stores(self, lat: float, lon: float, radius_km: float) -> list[Store]:
        """dm stores around a point, sorted by distance."""
        nw_lat, nw_lon, se_lat, se_lon = bbox_around(lat, lon, radius_km)
        url = DM_STORE_BBOX_URL.format(nw_lat=nw_lat, nw_lon=nw_lon, se_lat=se_lat, se_lon=se_lon)
        data = await self._get_json(url)
        stores = [store for s in data.get("stores", []) if (store := _parse_store(s))]
        for store in stores:
            store.distance_km = haversine_km(lat, lon, store.lat, store.lon)
        # The bbox is a square, so drop corner stores beyond the circular radius.
        stores = [s for s in stores if s.distance_km <= radius_km]
        stores.sort(key=lambda s: s.distance_km)
        return stores[:MAX_STORE_RESULTS]

    async def get_store(self, store_id: str) -> Store | None:
        data = await self._get_json(DM_STORE_ITEM_URL.format(store_id=store_id))
        return _parse_store(data)

    async def geocode(self, query: str) -> tuple[float, float, str] | None:
        """Resolve a German postal code or place name to coordinates."""
        query = query.strip()
        if re.fullmatch(r"\d{5}", query):
            params = {"postalcode": query, "countrycodes": "de", "format": "json", "limit": 1}
        else:
            params = {"q": query, "countrycodes": "de", "format": "json", "limit": 1}
        data = await self._get_json(NOMINATIM_URL, params=params)
        if not data:
            return None
        place = data[0]
        try:
            lat, lon = float(place["lat"]), float(place["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        return lat, lon, place.get("display_name", query)
