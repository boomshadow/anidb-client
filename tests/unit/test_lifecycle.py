"""Tests for what `init()` and `close()` acquire and give back.

SPEC-006 calls `close()` the clean shutdown, and ADR-008 records why a second
`init()` is refused rather than ignored or rebuilt. The property underneath both
is one sentence: after `close()` returns, the process is in the state it was in
before `init()` ran, and a failed `init()` leaves it there too.

That was not true. `init()` built the transport fifty lines before it opened the
cache, so a bad database URL raised with the UDP socket already bound and both of
its threads already running, owned by nothing the caller could reach -- and since
the source port is pinned and no longer shareable (ADR-007), the caller who
corrected the URL and tried again could not bind. `close()` meanwhile declared the
module globals and assigned none of them, so it stopped the transport and then
went on handing it out, and never disposed the cache engine at all.
"""

import logging
import socket

import pytest
from sqlalchemy.exc import NoSuchModuleError

import anidb_client
from anidb_client.errors import AniDBError


def free_port() -> int:
    """A port nothing is using, released before it is returned.

    Same approach the transport's own port tests take. Racy in principle; the
    alternative is a hard-coded port that collides with whatever else is running
    on the machine, which is worse and less obvious when it happens.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("", 0))
    port = holder.getsockname()[1]
    holder.close()
    return port


@pytest.fixture
def clean_globals(monkeypatch):
    """Reset the module state around each test, whatever the test leaves behind."""
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("_engine", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)
    yield
    anidb_client.close()


class RecordingEngine:
    """Stands in for the cache engine so disposal is observable."""

    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


class TestASecondInitIsRefused:
    """ADR-008. The alternatives are a silent no-op and a rebuild, and both are worse.

    Before this, a second call built a second transport that could not bind, and
    failed with an address-in-use error naming a port nothing visible was using --
    from a call that reads like configuration rather than like opening a socket.
    """

    def test_a_second_call_says_so(self, tmp_path, clean_globals):
        anidb_client.init(f"sqlite:///{tmp_path}/one.db", db_only=True)

        with pytest.raises(AniDBError) as raised:
            anidb_client.init(f"sqlite:///{tmp_path}/two.db", db_only=True)

        assert "already been called" in str(raised.value)

    def test_the_refusal_names_the_way_out(self, tmp_path, clean_globals):
        """A refusal a caller cannot act on is only half an answer."""
        anidb_client.init(f"sqlite:///{tmp_path}/one.db", db_only=True)

        with pytest.raises(AniDBError) as raised:
            anidb_client.init(f"sqlite:///{tmp_path}/two.db", db_only=True)

        assert "close()" in str(raised.value)

    def test_the_first_client_is_left_working(self, tmp_path, clean_globals):
        """Refusing must not damage what it refused on behalf of."""
        anidb_client.init(f"sqlite:///{tmp_path}/one.db", db_only=True)

        with pytest.raises(AniDBError):
            anidb_client.init(f"sqlite:///{tmp_path}/two.db", db_only=True)

        with anidb_client.get_session() as sess:
            assert sess is not None

    def test_closing_first_makes_a_second_call_legal(self, tmp_path, clean_globals):
        """The refusal is about one *live* client, not about one call per process."""
        anidb_client.init(f"sqlite:///{tmp_path}/one.db", db_only=True)
        anidb_client.close()
        anidb_client.init(f"sqlite:///{tmp_path}/two.db", db_only=True)

        with anidb_client.get_session() as sess:
            assert sess is not None


class TestAFailedInitLeavesNothingBehind:
    def test_a_bad_cache_url_opens_no_transport(self, tmp_path, clean_globals, monkeypatch):
        """The cache is opened first because it is the cheaper thing to fail.

        This is the ordering half of the fix. The transport used to be built first,
        so a URL the database layer refuses left a bound socket and two running
        threads behind a call that raised.
        """
        opened = []
        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: opened.append(kw))

        # A dialect SQLAlchemy cannot load: the database layer refuses it, which is
        # a failure arriving from below rather than one this module raises itself.
        with pytest.raises(NoSuchModuleError):
            anidb_client.init("nosuchdialect://host/db", api_user="u", api_pass="p")

        assert opened == [], "the transport must not be built before the cache has opened"

    def test_a_transport_that_cannot_be_built_gives_the_cache_back(self, tmp_path, clean_globals, monkeypatch):
        """The ordering half is not the whole fix: the second resource can fail too.

        `AniDBLink` binds its socket and starts its listener inside its own
        constructor, so it can raise with both already live. Ordering alone would
        leave the cache engine holding its pool in that case; the ExitStack unwinds
        it.
        """
        engine = RecordingEngine()
        monkeypatch.setattr(anidb_client.db, "init_db", lambda *a, **kw: (engine, object()))

        def refuse(*args, **kwargs):
            raise AniDBError("cannot bind")

        monkeypatch.setattr(anidb_client.link, "AniDBLink", refuse)

        with pytest.raises(AniDBError, match="cannot bind"):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")

        assert engine.disposed, "the cache engine must be disposed when init() fails after opening it"

    def test_the_library_is_still_uninitialised_afterwards(self, tmp_path, clean_globals, monkeypatch):
        """Which is what lets a caller correct the problem and try again."""
        monkeypatch.setattr(anidb_client.db, "init_db", lambda *a, **kw: (RecordingEngine(), object()))

        def refuse(*args, **kwargs):
            raise AniDBError("cannot bind")

        monkeypatch.setattr(anidb_client.link, "AniDBLink", refuse)

        with pytest.raises(AniDBError):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")

        assert anidb_client._anidb is None
        assert anidb_client._sessionmaker is None
        assert anidb_client._engine is None

    def test_a_corrected_call_succeeds(self, tmp_path, clean_globals, monkeypatch):
        """The end-to-end shape of the reported problem, minus the socket."""
        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: None)

        with pytest.raises(NoSuchModuleError):
            anidb_client.init("nosuchdialect://host/db", db_only=True)

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)
        with anidb_client.get_session() as sess:
            assert sess is not None

    def test_no_fanart_key_is_left_behind(self, tmp_path, clean_globals, monkeypatch):
        """A refused init() must not half-configure the library it refused to configure."""
        monkeypatch.setattr(anidb_client.db, "init_db", lambda *a, **kw: (RecordingEngine(), object()))

        def refuse(*args, **kwargs):
            raise AniDBError("cannot bind")

        monkeypatch.setattr(anidb_client.link, "AniDBLink", refuse)

        with pytest.raises(AniDBError):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p", fanart_api_key="secret")

        assert anidb_client.fanart_key is None


class TestCloseGivesBackWhatInitTook:
    def test_the_cache_engine_is_disposed(self, tmp_path, clean_globals, monkeypatch):
        engine = RecordingEngine()
        monkeypatch.setattr(anidb_client.db, "init_db", lambda *a, **kw: (engine, object()))

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)
        anidb_client.close()

        assert engine.disposed

    def test_the_session_factory_is_gone(self, tmp_path, clean_globals):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)
        anidb_client.close()

        with pytest.raises(AniDBError, match="init"):
            anidb_client.get_session()

    def test_the_fanart_key_is_cleared(self, tmp_path, clean_globals):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True, fanart_api_key="secret")
        assert anidb_client.fanart_key == "secret"

        anidb_client.close()

        assert anidb_client.fanart_key is None

    def test_the_transport_is_stopped(self, tmp_path, clean_globals, monkeypatch):
        class FakeLink:
            def __init__(self, *a, **kw):
                self.stopped = False

            def stop(self, timeout=None, logout=True):
                self.stopped = True

        built = []
        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: built.append(FakeLink()) or built[-1])

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")
        link = built[-1]
        anidb_client.close()

        assert link.stopped

    def test_a_stopped_transport_is_not_handed_out(self, tmp_path, clean_globals, monkeypatch):
        """The sharper half of the leak, because the answer looked plausible.

        `get_link()` exists so an embedder can read the health surface without
        sending anything (SPEC-002). Answering with a transport that has been
        stopped describes a session that no longer exists -- and the whole point of
        that surface is that it can be believed without checking.
        """

        class FakeLink:
            def stop(self, timeout=None, logout=True):
                pass

        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: FakeLink())

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")
        assert anidb_client.get_link() is not None

        anidb_client.close()

        with pytest.raises(AniDBError) as raised:
            anidb_client.get_link()
        assert "close()" in str(raised.value)

    def test_a_failure_stopping_the_transport_still_returns_the_pool(self, tmp_path, clean_globals, monkeypatch):
        """A transport that cannot be stopped must not also cost the connections."""
        engine = RecordingEngine()
        monkeypatch.setattr(anidb_client.db, "init_db", lambda *a, **kw: (engine, object()))

        class BadLink:
            def stop(self, timeout=None, logout=True):
                raise RuntimeError("stop went wrong")

        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: BadLink())

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")

        with pytest.raises(RuntimeError):
            anidb_client.close()

        assert engine.disposed
        # And the library is uninitialised regardless, so the caller who caught
        # that can still start again.
        assert anidb_client._anidb is None
        assert anidb_client._sessionmaker is None


class TestCloseIsSafeToOverdo:
    def test_closing_without_having_initialised_does_nothing(self, clean_globals):
        anidb_client.close()

    def test_closing_twice_does_nothing_the_second_time(self, tmp_path, clean_globals):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)
        anidb_client.close()
        anidb_client.close()

    def test_a_db_only_client_closes_cleanly(self, tmp_path, clean_globals):
        """There is no transport to stop, which must not be mistaken for an error."""
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)
        anidb_client.close()

        assert anidb_client._sessionmaker is None


class TestThePinnedPortComesBack:
    """The failure that was actually reported, end to end and with a real socket.

    Everything above stands in for the transport. This one builds a real
    `AniDBLink`, which binds a real UDP socket and starts both threads, and then
    asks the question a restarting or retrying caller asks: can the same port be
    bound again? Before `stop()` was fixed there was no path through it that closed
    the socket *and* stopped both threads -- authenticated, it sent LOGOUT and
    returned with the socket still bound; unauthenticated, it closed the socket and
    left the sender running forever.

    Nothing is sent here. The link never authenticates, and the sender's idle
    keepalive returns immediately while there is no session.
    """

    def test_the_port_can_be_bound_again_after_close(self, tmp_path, clean_globals):
        port = free_port()

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p", outgoing_udp_port=port)
        anidb_client.close()

        # The proof: a second client on the same port. This raised
        # "Cannot bind outgoing UDP port" before close() released the socket.
        anidb_client.init(f"sqlite:///{tmp_path}/cache2.db", api_user="u", api_pass="p", outgoing_udp_port=port)

    def test_the_socket_is_closed_before_close_returns(self, tmp_path, clean_globals):
        """Synchronously, not eventually -- a caller retrying cannot wait on a thread."""
        port = free_port()
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p", outgoing_udp_port=port)
        link = anidb_client.get_link()

        anidb_client.close()

        assert link._listener.sock is None


class TestConnectingAtStartup:
    """`connect()` is the third lifecycle call, and the only optional one.

    `init()` sends nothing -- the handshake is lazy -- so an application that
    wants a wrong credential or a standing ban to fail at boot rather than in
    front of a user needs a way to ask. Before this there was none, and the only
    route was to hand-build a command and push it through the transport, which
    meant importing a module below the declared public surface for a type the
    public API requires.
    """

    def test_it_is_part_of_the_declared_public_surface(self):
        """A caller told to use it has to be able to find it."""
        assert "connect" in anidb_client.__all__

    def test_it_reaches_the_transport(self, tmp_path, clean_globals, monkeypatch):
        class FakeLink:
            def __init__(self, *a, **kw):
                self.connected = 0

            def connect(self, timeout=None):
                self.connected += 1

            def stop(self, *a, **kw):
                pass

        built = []
        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: built.append(FakeLink()) or built[-1])

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")
        anidb_client.connect()

        assert built[-1].connected == 1

    def test_the_callers_bound_reaches_the_transport(self, tmp_path, clean_globals, monkeypatch):
        """An application whose startup budget is shorter than sixty seconds has to
        be able to say so, and the value has to actually arrive."""
        seen: list[object] = []

        class FakeLink:
            def connect(self, timeout=None):
                seen.append(timeout)

            def stop(self, *a, **kw):
                pass

        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: FakeLink())

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")
        anidb_client.connect(timeout=5)
        anidb_client.connect()

        assert seen == [5, None], "the bound must reach the transport, and omitting it must stay the default"

    def test_the_reason_it_could_not_connect_reaches_the_caller(self, tmp_path, clean_globals, monkeypatch):
        """Not swallowed: being told at startup is the entire point of the call."""

        class RefusingLink:
            def connect(self, timeout=None):
                raise AniDBError("AniDB refused these credentials")

            def stop(self, *a, **kw):
                pass

        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: RefusingLink())

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")

        with pytest.raises(AniDBError, match="refused these credentials"):
            anidb_client.connect()

    def test_a_cache_only_client_says_it_has_no_session_to_open(self, tmp_path, clean_globals):
        """db_only opens no UDP session and needs none, so asking is a configuration
        mistake worth naming rather than a silent no-op."""
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)

        with pytest.raises(AniDBError) as raised:
            anidb_client.connect()

        assert "db_only" in str(raised.value)

    def test_connecting_before_init_says_so(self, clean_globals):
        with pytest.raises(AniDBError, match="init"):
            anidb_client.connect()

    def test_connecting_after_close_says_so(self, tmp_path, clean_globals, monkeypatch):
        """`close()` returns the library to its pre-init state, and that includes
        having nothing to connect."""

        class FakeLink:
            def connect(self, timeout=None):
                pass

            def stop(self, *a, **kw):
                pass

        monkeypatch.setattr(anidb_client.link, "AniDBLink", lambda *a, **kw: FakeLink())

        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")
        anidb_client.close()

        with pytest.raises(AniDBError, match=r"close\(\)"):
            anidb_client.connect()
