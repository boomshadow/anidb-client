"""Tests for how init() opens the cache database.

Two rules live here. The connection pool is bounded and the bound is the caller's
to move; and an in-memory cache is refused outside `db_only`, because a
`:memory:` database is per-connection and this library hands connections to
response-callback threads that would each find an empty one.
"""

import logging

import pytest

import anidb_client
from anidb_client.db import POOL_MAX_OVERFLOW


@pytest.fixture
def clean_globals(monkeypatch):
    """init() writes module globals; restore them regardless of what happens."""
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)


@pytest.fixture
def opened_cache(clean_globals):
    """Run init() in cache-only mode and dispose whatever engine it left behind."""

    def go(url, **kwargs):
        anidb_client.init(url, db_only=True, **kwargs)
        return anidb_client._sessionmaker

    yield go

    factory = anidb_client._sessionmaker
    bind = factory.kw.get("bind") if factory is not None else None
    if bind is not None:
        bind.dispose()


class TestTheInMemoryGuard:
    def test_in_memory_without_db_only_is_refused(self, clean_globals):
        """It used to succeed and then silently answer nothing useful."""
        with pytest.raises(anidb_client.errors.AniDBError, match="in-memory"):
            anidb_client.init("sqlite://", api_user="u", api_pass="p")

    def test_the_refusal_names_the_reason(self, clean_globals):
        with pytest.raises(anidb_client.errors.AniDBError, match="own empty database"):
            anidb_client.init("sqlite:///:memory:", api_user="u", api_pass="p")

    def test_no_udp_link_is_opened_before_the_refusal(self, clean_globals, monkeypatch):
        """The guard is the first thing init() does, so nothing is left running."""
        opened = []
        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: opened.append(kw))

        with pytest.raises(anidb_client.errors.AniDBError):
            anidb_client.init("sqlite://", api_user="u", api_pass="p")

        assert opened == []

    def test_in_memory_with_db_only_still_works(self, opened_cache):
        """The realistic use, and the one this keeps: no threads, no UDP session."""
        factory = opened_cache("sqlite://")
        with factory() as sess:
            assert sess is not None

    def test_a_file_backed_url_is_not_refused(self, tmp_path, opened_cache):
        factory = opened_cache(f"sqlite:///{tmp_path}/cache.db")
        assert factory is not None


class TestThePoolBound:
    def test_the_default_bound_reaches_the_engine(self, tmp_path, opened_cache):
        factory = opened_cache(f"sqlite:///{tmp_path}/cache.db")
        pool = factory.kw["bind"].pool
        assert pool.size() == 10
        assert pool._max_overflow == POOL_MAX_OVERFLOW

    def test_the_override_reaches_the_engine(self, tmp_path, opened_cache):
        factory = opened_cache(f"sqlite:///{tmp_path}/cache.db", db_pool_size=2)
        assert factory.kw["bind"].pool.size() == 2
