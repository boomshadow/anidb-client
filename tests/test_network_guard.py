"""Tests for the network guard in conftest.py.

The guard is the reason this suite is safe to run repeatedly. If it silently
stopped working, nothing else would fail -- the tests would simply start talking
to AniDB. So it gets tested like any other safety-critical code.
"""

import socket
import urllib.request

import pytest

from tests.conftest import ExternalNetworkBlocked


def test_udp_sendto_a_public_address_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(ExternalNetworkBlocked, match="not loopback"):
        sock.sendto(b"AUTH user=x", ("api.anidb.net", 9000))


def test_udp_sendto_a_public_ip_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(ExternalNetworkBlocked):
        sock.sendto(b"PING", ("1.1.1.1", 9000))


def test_tcp_connect_to_a_public_address_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(ExternalNetworkBlocked):
        sock.connect(("anidb.net", 443))


def test_name_resolution_is_blocked():
    """Resolution is itself traffic, so it is refused before a connection exists."""
    with pytest.raises(ExternalNetworkBlocked, match="resolve"):
        socket.getaddrinfo("api.anidb.net", 9000)


def test_urlopen_is_blocked():
    with pytest.raises(ExternalNetworkBlocked, match="anime-titles"):
        urllib.request.urlopen("https://anidb.net/api/anime-titles.xml.gz")


def test_urlopen_of_a_request_object_is_blocked():
    req = urllib.request.Request("https://webservice.fanart.tv/v3/tv/81797")
    with pytest.raises(ExternalNetworkBlocked):
        urllib.request.urlopen(req)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_is_allowed(host):
    """The guard must not block the fake AniDB server the suite depends on."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = socket.socket(family, socket.SOCK_DGRAM)
    server.bind((host, 0))
    try:
        port = server.getsockname()[1]
        client = socket.socket(family, socket.SOCK_DGRAM)
        try:
            client.sendto(b"PING", (host, port))
            server.settimeout(5)
            assert server.recv(64) == b"PING"
        finally:
            client.close()
    finally:
        server.close()


def test_guard_is_autouse_and_needs_no_opt_in():
    """Nothing in this module requested the fixture, yet the calls above were blocked.

    This is what makes the guard trustworthy: a newly added test file is covered
    by default rather than when its author remembers to ask.
    """
    assert socket.socket.__name__ == "GuardedSocket"
