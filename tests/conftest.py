"""Shared test fixtures, and the guard that keeps this suite off AniDB.

AniDB's UDP API bans by IP, and its HTTP endpoints (anime-titles.xml.gz in
particular) ban for fetching too often. A test that accidentally reaches the real
service does not fail loudly -- it succeeds once and then gets whoever runs the
suite next banned for 24 hours.

So every test runs behind `block_external_network`, an autouse fixture that lets
loopback through and turns anything else into an immediate error. It is deliberately
belt-and-braces: it guards the address a socket is pointed at, name resolution, and
urllib's entry points, because the library reaches the network by all three routes.
"""

import ipaddress
import logging
import os
import socket
import urllib.request

import pytest

from tests import factories
from tests.objectlayer import RecordingLink

# Hostnames allowed without resolution. Anything else must already be a loopback
# literal, so a test cannot reach the network by way of a name we never resolved.
_ALLOWED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", ""})


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test tries to talk to anything that is not loopback."""


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if not isinstance(host, str):
        # AF_UNIX and friends carry no host at all; they are not the network.
        return True
    if host in _ALLOWED_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        # A hostname we have not explicitly allowed. Refuse it rather than
        # resolving, since resolving is itself traffic.
        return False


def _check(address, what):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_loopback(host):
        raise ExternalNetworkBlocked(
            f"This test tried to {what} {host!r}, which is not loopback.\n"
            f"The suite must never contact AniDB: it bans by IP, and a ban lands on "
            f"whoever runs the tests next. Use the fake AniDB server fixture instead."
        )


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Fail any test that addresses a non-loopback host. Applied to every test."""
    real_socket_cls = socket.socket
    real_getaddrinfo = socket.getaddrinfo

    class GuardedSocket(real_socket_cls):
        def connect(self, address):
            _check(address, "connect to")
            return super().connect(address)

        def connect_ex(self, address):
            _check(address, "connect to")
            return super().connect_ex(address)

        def sendto(self, data, *args):
            # sendto(data, address) and sendto(data, flags, address) are both valid.
            _check(args[-1] if args else None, "send a UDP packet to")
            return super().sendto(data, *args)

        def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if address is not None:
                _check(address, "send a UDP packet to")
            return super().sendmsg(buffers, ancdata, flags, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        _check(host, "resolve")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        _check(address, "connect to")
        raise ExternalNetworkBlocked("socket.create_connection is not available in tests")

    def guarded_urlopen(url, *args, **kwargs):
        target = getattr(url, "full_url", url)
        raise ExternalNetworkBlocked(
            f"This test tried to fetch {target!r} over HTTP.\n"
            f"AniDB bans clients that re-fetch anime-titles.xml.gz too often. "
            f"Patch the fetch, or point it at a local file."
        )

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded_urlopen)
    yield


# ---------------------------------------------------------------------------
# Object-layer fixtures
# ---------------------------------------------------------------------------
# The library keeps its session in module globals -- `init()` sets `log`,
# `_anidb`, `_sessionmaker` and `fanart_key`. There is no session object to pass
# around, so these fixtures set those globals and let monkeypatch restore them.
# Each is set *before* init() runs, so the value monkeypatch records is the one
# from before the test rather than one init() left behind.


@pytest.fixture
def cache_url(tmp_path):
    """A throwaway SQLite cache file, one per test."""
    return f"sqlite:///{tmp_path}/cache.db"


@pytest.fixture
def link():
    """The recording stand-in installed as the library's AniDB link."""
    return RecordingLink()


@pytest.fixture
def anidb(cache_url, link, monkeypatch):
    """The library, initialised offline: real cache, recording link, no network.

    Yields the `anidb_client` module. Objects built under this fixture take their
    ordinary code paths; nothing in the package is patched. The only things
    supplied are the cache and the two XML documents the library would otherwise
    download.
    """
    import anidb_client

    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)

    factories.install_title_data(monkeypatch)
    factories.install_anime_list(monkeypatch)

    # db_only: opens the cache without opening a UDP session or wanting credentials.
    anidb_client.init(cache_url, db_only=True)
    monkeypatch.setattr(anidb_client, "_anidb", link)

    yield anidb_client

    # Dispose the engine explicitly. init_db() builds one per call and nothing owns
    # it afterwards, so without this each test leaks its pooled SQLite connections
    # until the garbage collector gets to them -- which surfaces as a drift of
    # ResourceWarnings and, on a server database, as connections held open.
    bind = anidb_client._sessionmaker.kw.get("bind")
    if bind is not None:
        bind.dispose()


@pytest.fixture
def session(anidb):
    """A session on the same cache the library is using, for seeding rows."""
    sess = anidb.get_session()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def postgres_url():
    """URL of a real PostgreSQL, or skip.

    Supplied by docker-compose and by CI. Absent locally outside compose, in which
    case the schema tests that need a server database skip rather than fail -- CI
    separately asserts that they did run.
    """
    url = os.environ.get("ANIDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("ANIDB_TEST_POSTGRES_URL is not set; start the compose `db` service")
    return url
