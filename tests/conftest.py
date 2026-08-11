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
import socket
import urllib.request

import pytest

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
