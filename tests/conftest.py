"""Shared fixtures for dm availability bot tests."""

import pytest

from app import storage


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch, tmp_path):
    """Redirect DB_PATH to a temporary database for all tests."""
    monkeypatch.setattr("app.config.DB_PATH", str(tmp_path / "bot.db"))
    storage.init_db()


def make_tile(store_icon="GREEN", store_text="<linking>Dein dm-Markt</linking> (11)",
              online_icon="GREEN", online_text="Lieferbar"):
    """Build a synthetic availability tile as returned by the dm API."""
    return {
        "rows": [
            {"icon": online_icon, "iconLabel": "Status", "text": online_text},
            {"icon": store_icon, "iconLabel": "Status", "text": store_text},
        ],
        "isPurchasable": True,
    }
