"""Tests for the public accessor that hands out the transport.

SPEC-002 gives the transport a read-only health surface so that an embedding
application can tell a client with nothing to do from a client AniDB has gated,
without sending anything to find out. That surface is only reachable if the
object carrying it is: before this accessor existed the only handles on the
transport were a module-private global and the object layer's private
`_anidb_link`, so consulting it meant the private-attribute reach-in the surface
exists to spare a caller.
"""

import logging

import pytest

import anidb_client
from anidb_client.errors import AniDBError


@pytest.fixture
def init_link(monkeypatch, tmp_path):
    """Run init() for real, standing a sentinel in for the link it would open.

    Nothing here should bind the pinned port: a test that did would fight every
    other client on the machine for it, which is the collision the pinned default
    is designed to make visible rather than something to provoke here.
    """
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("_engine", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)

    class FakeLink:
        """A stand-in with the one method the lifecycle calls on it."""

        stopped = False

        def stop(self, *args, **kwargs):
            type(self).stopped = True

    sentinel = FakeLink()
    monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: sentinel)

    calls = []

    def go(**kwargs):
        cache = tmp_path / f"cache{len(calls)}.db"
        calls.append(cache)
        anidb_client.init(f"sqlite:///{cache}", api_user="u", api_pass="p", **kwargs)
        return sentinel

    yield go

    anidb_client.close()


class TestReachingTheTransport:
    def test_the_transport_is_handed_out(self, init_link):
        """The point of the accessor: the health surface is reachable publicly."""
        sentinel = init_link()
        assert anidb_client.get_link() is sentinel

    def test_it_is_part_of_the_declared_public_surface(self):
        """A caller told to use it must be able to find it in `__all__`."""
        assert "get_link" in anidb_client.__all__


class TestWhenThereIsNoTransport:
    """Asking for a transport that does not exist says which reason it does not.

    Answering None instead would push a branch onto every call site for something
    that is a statement about how this process was configured, not a condition a
    caller should have to handle each time it reads the health surface.
    """

    def test_before_init_it_says_so(self, monkeypatch):
        monkeypatch.setattr(anidb_client, "_anidb", None, raising=False)
        with pytest.raises(AniDBError) as excinfo:
            anidb_client.get_link()
        assert "init()" in str(excinfo.value)

    def test_a_cache_only_client_has_none_by_construction(self, init_link):
        """db_only opens no UDP session, so there is no transport and never will be."""
        init_link(db_only=True)
        with pytest.raises(AniDBError) as excinfo:
            anidb_client.get_link()
        assert "db_only" in str(excinfo.value)
