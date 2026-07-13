"""Tests for dm API response parsing and geo helpers."""

import httpx
import pytest

from app.dm_api import DmApi, _parse_store, bbox_around, haversine_km, parse_tile
from tests.conftest import make_tile


class TestParseStore:
    def test_valid(self):
        store = _parse_store(
            {
                "storeId": "D357",
                "address": {"street": "Hauptstr. 1", "zip": "44649", "city": "Herne"},
                "location": {"lat": 51.5, "lon": 7.2},
            }
        )
        assert store is not None
        assert store.store_id == "D357"
        assert store.city == "Herne"
        assert (store.lat, store.lon) == (51.5, 7.2)

    def test_missing_store_id_is_none(self):
        assert _parse_store({"address": {"city": "Herne"}}) is None
        assert _parse_store({}) is None

    def test_bad_coords_coerced_to_zero(self):
        store = _parse_store({"storeId": "D1", "location": {"lat": "n/a", "lon": None}})
        assert store is not None
        assert (store.lat, store.lon) == (0.0, 0.0)


class TestSearchProducts:
    async def test_skips_non_numeric_dan(self, monkeypatch):
        api = DmApi()

        async def fake_get_json(url, params=None):
            return {
                "products": [
                    {"dan": "abc", "title": "bad"},
                    {"dan": "100", "brandName": "b", "title": "good"},
                    {"title": "no dan"},
                ]
            }

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        products = await api.search_products("x")
        assert [p.dan for p in products] == [100]


class TestFindStores:
    async def test_filters_beyond_radius_and_sorts(self, monkeypatch):
        api = DmApi()

        async def fake_get_json(url, params=None):
            return {
                "stores": [
                    {"storeId": "F", "location": {"lat": 52.5, "lon": 7.2}},  # ~111 km
                    {"storeId": "N", "location": {"lat": 51.5, "lon": 7.2}},  # 0 km
                    {"storeId": "M", "location": {"lat": 51.55, "lon": 7.2}},  # ~5.5 km
                ]
            }

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        stores = await api.find_stores(51.5, 7.2, 10)
        assert [s.store_id for s in stores] == ["N", "M"]  # F dropped, sorted by distance


class TestGetStore:
    async def test_get_store_returns_none_on_malformed(self, monkeypatch):
        api = DmApi()

        async def fake_get_json(url, params=None):
            return {"no": "storeId"}

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        assert await api.get_store("D999") is None


class TestParseTile:
    def test_in_stock_with_count(self):
        a = parse_tile(123, make_tile(store_icon="GREEN", store_text="<linking>Dein dm-Markt</linking> (11)"))
        assert a.store_available is True
        assert a.store_stock == 11
        assert a.online_available is True

    def test_out_of_stock_red(self):
        a = parse_tile(123, make_tile(store_icon="RED", store_text="<linking>Dein dm-Markt</linking>"))
        assert a.store_available is False
        assert a.store_stock is None

    def test_zero_stock_counts_as_unavailable(self):
        a = parse_tile(123, make_tile(store_icon="GREEN", store_text="Dein dm-Markt (0)"))
        assert a.store_available is False
        assert a.store_stock == 0

    def test_stock_count_wins_over_icon(self):
        a = parse_tile(123, make_tile(store_icon="YELLOW", store_text="Dein dm-Markt (2)"))
        assert a.store_available is True
        assert a.store_stock == 2

    def test_online_not_available(self):
        a = parse_tile(123, make_tile(online_icon="RED", online_text="Nicht lieferbar"))
        assert a.online_available is False

    def test_no_store_row(self):
        a = parse_tile(123, {"rows": [{"icon": "GREEN", "text": "Lieferbar"}]})
        assert a.store_available is None
        assert a.online_available is True

    def test_empty_tile(self):
        a = parse_tile(123, {})
        assert a.store_available is None
        assert a.store_stock is None
        assert a.online_available is None


class TestGeo:
    def test_bbox_orientation(self):
        nw_lat, nw_lon, se_lat, se_lon = bbox_around(51.5, 7.2, 10)
        assert nw_lat > 51.5 > se_lat
        assert nw_lon < 7.2 < se_lon

    def test_bbox_scales_with_radius(self):
        small = bbox_around(51.5, 7.2, 5)
        large = bbox_around(51.5, 7.2, 20)
        assert large[0] - large[2] > small[0] - small[2]

    def test_haversine_known_distance(self):
        # Cologne -> Dusseldorf is ~34 km
        d = haversine_km(50.9375, 6.9603, 51.2277, 6.7735)
        assert 30 < d < 40

    def test_haversine_zero(self):
        assert haversine_km(51.5, 7.2, 51.5, 7.2) == 0


class TestGetAvailability:
    async def test_batches_and_parses(self, monkeypatch):
        api = DmApi()
        requested_urls = []

        async def fake_get_json(url, params=None):
            requested_urls.append(url)
            dans = url.rsplit("/", 1)[1].split(",")
            return {dan: make_tile() for dan in dans}

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        monkeypatch.setattr("app.dm_api.AVAILABILITY_BATCH_SIZE", 2)

        result = await api.get_availability("D357", [1, 2, 3])
        assert len(requested_urls) == 2  # 2 + 1 dans
        assert set(result) == {1, 2, 3}
        assert result[1].store_available is True

    async def test_missing_dan_omitted(self, monkeypatch):
        api = DmApi()

        async def fake_get_json(url, params=None):
            return {"1": make_tile()}

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        result = await api.get_availability("D357", [1, 99999])
        assert 1 in result
        assert 99999 not in result


class TestRateLimit:
    async def test_requests_are_spaced(self, monkeypatch):
        monkeypatch.setattr("app.dm_api.DM_MIN_REQUEST_INTERVAL", 0.05)
        api = DmApi()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        import time

        start = time.monotonic()
        await api._get_json("https://example.invalid/a")
        await api._get_json("https://example.invalid/b")
        assert time.monotonic() - start >= 0.05
        await api.aclose()


class TestGeocode:
    @pytest.mark.parametrize(
        "query,expected_param",
        [("76133", "postalcode"), ("Karlsruhe", "q"), (" 76133 ", "postalcode")],
    )
    async def test_plz_vs_city(self, monkeypatch, query, expected_param):
        api = DmApi()
        seen = {}

        async def fake_get_json(url, params=None):
            seen.update(params)
            return [{"lat": "49.0", "lon": "8.4", "display_name": "Karlsruhe"}]

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        result = await api.geocode(query)
        assert expected_param in seen
        assert result == (49.0, 8.4, "Karlsruhe")

    async def test_no_result(self, monkeypatch):
        api = DmApi()

        async def fake_get_json(url, params=None):
            return []

        monkeypatch.setattr(api, "_get_json", fake_get_json)
        assert await api.geocode("nowhere") is None
