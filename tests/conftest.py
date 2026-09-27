"""Shared fixtures: every test session gets a fresh SQLite DB built from the
seed files in a temp folder, with the demo date pinned to 2026-10-01."""

from __future__ import annotations

import pytest

from facility_ops.config import get_settings
from facility_ops.data_access import db
from facility_ops.data_access.knowledge_base import _cached_index


def _reset_caches():
    get_settings.cache_clear()
    _cached_index.cache_clear()
    db._verified_path = None


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A brand-new database per test (tests that create tickets stay isolated)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AS_OF_DATE", "2026-10-01")
    monkeypatch.setenv("DUE_SOON_DAYS", "30")
    monkeypatch.setenv("UPCOMING_DAYS", "90")
    monkeypatch.setenv("COVERAGE_EXPIRING_DAYS", "60")
    monkeypatch.setenv("KB_MIN_SCORE", "2.0")
    _reset_caches()
    db.build_database(reset=True)
    yield
    _reset_caches()
