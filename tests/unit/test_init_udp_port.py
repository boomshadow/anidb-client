"""Tests for the outgoing UDP source port `init()` hands the transport.

The port is not an implementation detail. AniDB counts requests against a source
address, so which port this client sends from is part of the identity it is rate
limited and banned by. `init()` used to pick a fresh random one per call; the
incident behind ADR-007 was a client constructed per operation across short-lived
processes, which presented dozens of distinct source ports from one IP inside an
hour and was banned for it while every individual process behaved impeccably.
"""

import logging

import pytest

import anidb_client
from anidb_client.link import DEFAULT_OUTGOING_PORT


@pytest.fixture
def opened_links(monkeypatch, tmp_path):
    """Run init() for real, but record the link's arguments instead of opening one.

    A test that actually bound the pinned port would fight every other test on
    the machine for it, which is the very collision this default is designed to
    make visible rather than something to provoke here.
    """
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("_engine", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)

    opened: list[dict[str, object]] = []

    class FakeLink:
        """Records the arguments and binds nothing.

        A class rather than the lambda this was, because `init()` now registers a
        teardown against the object it built -- so the stand-in has to be an object
        with a `stop()`, which is what a constructor returns and what the lambda,
        answering the return value of `list.append`, never was.
        """

        def __init__(self, *args, **kwargs):
            opened.append(kwargs)

        def stop(self, *args, **kwargs):
            pass

    monkeypatch.setattr(anidb_client.link, "AniDBLink", FakeLink)

    def go(**kwargs):
        cache = tmp_path / f"cache{len(opened)}.db"
        anidb_client.init(f"sqlite:///{cache}", api_user="u", api_pass="p", **kwargs)
        return opened

    yield go

    # One call, where this used to collect every engine init() built and dispose
    # them one by one. close() gives back what init() took, which is the whole
    # point of the change these tests sit alongside.
    anidb_client.close()


class TestTheDefaultPort:
    def test_the_default_is_the_pinned_port(self, opened_links):
        assert opened_links()[-1]["myport"] == DEFAULT_OUTGOING_PORT

    def test_the_default_does_not_move_between_calls(self, opened_links):
        """The whole point: two clients in a row present the same source port.

        The previous default rolled a new one per `init()`, so a process that
        built a client per operation looked to AniDB like a stream of distinct
        clients from one address.
        """
        opened_links()
        # init() refuses a second call while one is live (ADR-008), so the cycle
        # under test is init/close/init -- which is also the real-world shape of
        # this question: a service restarting must present the same port it did
        # before, not merely a second client in one process.
        anidb_client.close()
        opened = opened_links()

        assert len(opened) == 2
        assert opened[0]["myport"] == opened[1]["myport"] == DEFAULT_OUTGOING_PORT

    def test_the_pinned_port_is_one_the_kernel_will_not_reassign(self, opened_links):
        """Above 1024 so no privilege is needed, below the ephemeral floor so
        nothing else on the host is handed it by accident."""
        assert 1024 < DEFAULT_OUTGOING_PORT < 32768

    def test_a_caller_may_still_choose_its_own(self, opened_links):
        """Several clients on one host need several ports, and only the caller
        knows how many it is running."""
        assert opened_links(outgoing_udp_port=9877)[-1]["myport"] == 9877
