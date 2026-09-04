"""Transport tests against a real loopback UDP server.

These cover the paths that only exist because this is a network client: tag
correlation, deflate-compressed replies, session loss and reauthentication, and
the ban handling that must never be provoked against the real service.
"""

import contextlib
import logging
import socket
import threading
import time
from concurrent.futures import Future
from time import monotonic

import pytest

import anidb_client
import anidb_client.commands
from anidb_client.errors import (
    AniDBAuthFailedError,
    AniDBBannedError,
    AniDBCommandTimeoutError,
    AniDBError,
    BanCause,
)
from anidb_client.link import AniDBLink, AniDBListener
from anidb_client.ratelimit import RateLimiter
from tests.fake_anidb import FakeAniDBServer

AUTH_OK = "200 sess1234 127.0.0.1:9000 LOGIN ACCEPTED"


@pytest.fixture
def server():
    with FakeAniDBServer() as s:
        yield s


@pytest.fixture
def make_link(monkeypatch):
    """Build AniDBLinks pointed at the fake server, and shut them down after.

    The rate limiter is given a no-op sleep: the pacing policy has its own tests
    with an injected clock, and real 2-4 second waits here would buy nothing but
    a slow suite.
    """
    monkeypatch.setattr(anidb_client, "log", logging.getLogger("anidb_client.test"), raising=False)

    links = []

    def factory(srv, **kwargs):
        kwargs.setdefault("client_name", "anidbclientpy")
        kwargs.setdefault("client_version", 1)
        link = AniDBLink(
            "user",
            "pw",
            host=srv.host,
            port=srv.port,
            # Ephemeral unless a test is about the port itself.
            myport=kwargs.pop("myport", 0),
            timeout=kwargs.pop("timeout", 2),
            # Overridable: a test about the back-off needs a limiter that really
            # sleeps, or it cannot tell sleeping from not sleeping.
            rate_limiter=kwargs.pop("rate_limiter", None) or RateLimiter(sleep=lambda _seconds: None),
            **kwargs,
        )
        links.append(link)
        return link

    yield factory

    for link in links:
        # `stop(logout=False)` rather than reaching past it to the listener. That
        # reach-in was written when the sender genuinely had no stop short of a
        # LOGOUT round trip, so every test in this file left one running; the
        # transport can now be ended without sending anything, which is what
        # `TestStoppingTheTransport` below pins and what SPEC-002 promises.
        #
        # `logout=False` because a link built for a test has no session worth
        # ending, and several of these tests deliberately provoke a ban -- where
        # sending is the one thing that must not happen.
        #
        # Suppressed because a link whose transport a test already stopped has
        # nothing left to tear down, and because a test that provoked a ban may
        # leave the sender mid-error on a socket this is closing. Teardown noise
        # either way: both threads are daemons and both handle it.
        with contextlib.suppress(Exception):
            link.stop(logout=False)


def _await(predicate, timeout=5.0, message="condition never became true"):
    event = threading.Event()
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        event.wait(0.02)
        waited += 0.02
    raise AssertionError(message)


class TestAuthentication:
    def test_auth_is_sent_with_the_registered_client_identity(self, server, make_link):
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()

        request = server.wait_for("AUTH")
        assert request["fields"]["client"] == "anidbclientpy"
        assert request["fields"]["clientver"] == "1"
        assert request["fields"]["protover"] == "3"

    def test_the_session_key_from_the_reply_is_retained(self, server, make_link):
        """Every later command carries it as s=; without it they are rejected."""
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._session == "sess1234", message="session key was never stored")
        assert link._authed.is_set()

    def test_a_login_without_a_usable_address_still_authenticates(self, server, make_link):
        """The NAT check must not be able to fail the login it is only advising.

        AniDB returns the address it saw only when AUTH asked for it, and the
        handler read the field with a subscript and split it unconditionally. A
        reply that omitted it, or carried something that is not `ip:port`, raised
        on a response thread before the session was ever marked up -- so the login
        succeeded on the wire and hung the client anyway.
        """
        server.on("AUTH", "200 sess1234 LOGIN ACCEPTED")
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._authed.is_set(), message="a login with no address never completed")
        assert link._session == "sess1234"
        assert not link._do_ping, "an unreadable address must not be taken as evidence of NAT"

    def test_credentials_are_sent_but_never_logged(self, server, make_link, caplog):
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        with caplog.at_level(logging.DEBUG, logger="anidb_client.test"):
            link.reauthenticate()
            server.wait_for("AUTH")

        assert "pw" not in caplog.text.replace("anidbclientpy", "")

    def test_nat_is_detected_when_the_reported_port_differs(self, server, make_link):
        """AniDB echoes the address it saw. A different port means a NAT rewrote it.

        The client then has to send periodic keepalives or the mapping expires and
        replies stop arriving.
        """
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._authed.is_set(), message="never authenticated")
        assert link._do_ping is True

    def test_nat_is_not_detected_when_the_port_matches(self, server, make_link):
        link = make_link(server)
        bound_port = link._listener.sock.getsockname()[1]
        server.on("AUTH", lambda req: f"200 sess1234 127.0.0.1:{bound_port} LOGIN ACCEPTED")
        link._myport = bound_port

        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")
        assert link._do_ping is False


class TestTheSourcePortIsPinned:
    """One client, one socket, one identity on the wire.

    AniDB counts requests against a source address, so which port this client
    sends from is part of the identity it is banned by. The socket used to be
    bound with SO_REUSEADDR, which on Linux lets a second process bind a port
    this one already holds -- with no error on either side. The kernel then gives
    each datagram to one of them, so the starved client sees replies go missing,
    retries into the silence, and earns a ban for a collision it cannot see.
    """

    def test_a_second_bind_of_the_same_port_fails_loudly(self, server, make_link):
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("", 0))
        port = holder.getsockname()[1]

        try:
            with pytest.raises(AniDBError) as raised:
                make_link(server, myport=port)
        finally:
            holder.close()

        # Named, not just refused: a bare EADDRINUSE out of a library constructor
        # says nothing about which port or why this client insists on one.
        assert str(port) in str(raised.value)
        assert "source port" in str(raised.value)

    def test_a_link_binds_the_port_it_was_given(self, server, make_link):
        """The pinned default is only worth having if it is what reaches the socket."""
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("", 0))
        port = holder.getsockname()[1]
        holder.close()

        link = make_link(server, myport=port)

        assert link._listener.sock is not None
        assert link._listener.sock.getsockname()[1] == port


class TestTagCorrelation:
    def test_a_reply_is_delivered_to_the_command_that_asked(self, server, make_link):
        server.on("AUTH", AUTH_OK)
        server.on("PING", "300 PONG")
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        got = []
        link.request(anidb_client.commands.PingCommand(), got.append)

        _await(lambda: got, message="PING callback never fired")
        assert got[0].rescode == "300"

    def test_a_reply_with_an_unknown_tag_is_ignored(self, server, make_link):
        """UDP has no ordering, so a stale reply for a dropped command may arrive.

        It must not be handed to whichever command happens to be pending.
        """
        server.on("AUTH", AUTH_OK)
        server.on("PING", lambda req: b"T999 300 PONG\n")
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        got = []
        link.request(anidb_client.commands.PingCommand(), got.append)
        server.wait_for("PING")

        threading.Event().wait(0.3)
        assert got == [], "a reply with a foreign tag was delivered anyway"


class TestWireFormats:
    def test_a_deflate_compressed_reply_is_decompressed(self, server, make_link):
        """AUTH sends comp=1, so the server may deflate any reply."""
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        server.compress = True
        server.on("PING", "300 PONG")

        got = []
        link.request(anidb_client.commands.PingCommand(), got.append)
        _await(lambda: got, message="compressed PING reply was never decoded")
        assert got[0].rescode == "300"


class TestBanHandling:
    @pytest.mark.parametrize("code", [555, 600, 601, 602, 604])
    def test_an_untagged_server_error_registers_a_back_off(self, server, make_link, code):
        """These arrive with no tag, so there is no command to attribute them to.

        The client has to recognise them from the code alone and back off.

        555 is the one that matters: it is what AniDB actually answered with in
        the incident this test exists for, and the transport's hardcoded list of
        ban codes did not contain it. The reply was logged as unrecognised, no
        back-off was registered, and the client kept sending at full rate into a
        service that had just told it to stop.
        """
        server.on("AUTH", lambda req: f"{code} SERVER UNHAPPY\n".encode())
        link = make_link(server)
        link.reauthenticate()

        _await(
            lambda: link._rate_limiter.is_banned,
            message=f"code {code} did not trigger a back-off",
        )
        assert link._rate_limiter.ban_multiplier >= 1


class TestTaggedRefusals:
    """A refusal is a statement about the connection, not about one command.

    The transport used to decide that by looking at the tag first: an untagged
    reply was classified from the response table, and a tagged one was matched to
    its command and delivered. So a `555 BANNED` that arrived carrying a tag was
    handed to a caller as a perfectly ordinary successful response -- no ban
    registered, no back-off opened, and the sender still firing at full rate into
    a service that had just said stop.

    AniDB's own documentation notes that 555 "sometimes uses the 'wrong' tag", so
    this is not a hypothetical shape for the reply to arrive in. Classification
    now happens before correlation, which makes all three shapes -- untagged,
    correctly tagged, and tagged with something nothing is waiting on -- close the
    same gate.
    """

    @pytest.mark.parametrize("code", ["555", "600", "601", "602", "604"])
    def test_a_refusal_carrying_a_tag_closes_the_gate(self, server, make_link, code):
        server.on("PING", f"{code} SERVER UNHAPPY")
        link = make_link(server)

        link.request(anidb_client.commands.PingCommand(), lambda _resp: None)

        _await(lambda: link.is_banned, message=f"a tagged {code} registered no ban")
        assert link.ban_remaining > 0, "a ban was registered but no back-off window opened"
        assert link.ban_cause is BanCause.REFUSED

    @pytest.mark.parametrize("code", ["555", "600", "601", "602", "604"])
    def test_a_refusal_carrying_a_tag_settles_the_command_it_answered(self, server, make_link, code):
        """The other half of it: closing the gate must not strand the caller.

        SPEC-002's rule is that every request carries an outcome -- the reply, or
        the reason there will not be one. A refusal that shut the sender up and
        left its command in flight would trade one hang for another.
        """
        server.on("PING", f"{code} SERVER UNHAPPY")
        link = make_link(server)

        future = link.request(anidb_client.commands.PingCommand(), lambda _resp: None)

        with pytest.raises(AniDBBannedError) as raised:
            future.result(timeout=5)
        assert raised.value.rescode == code
        assert raised.value.cause is BanCause.REFUSED
        assert raised.value.retry_after > 0

    def test_a_refusal_carrying_the_wrong_tag_closes_the_gate(self, server, make_link):
        """The case AniDB actually documents, and the one a tag-first transport misses.

        Nothing is waiting on this tag, so there is no command to attribute the
        reply to and nothing to deliver it as. Under tag-first classification
        that made it a reply to nothing, silently dropped. It is still the API
        saying stop.
        """
        server.on("PING", lambda req: b"T999 555 BANNED\n")
        link = make_link(server)

        link.request(anidb_client.commands.PingCommand(), lambda _resp: None)

        _await(lambda: link.is_banned, message="a refusal tagged for nothing was dropped")
        assert link.ban_cause is BanCause.REFUSED


class TestSilenceIsABan:
    """The refusal that arrives as no packet at all.

    AniDB's primary enforcement is not an error reply: it drops datagrams from a
    client it has had enough of. A transport that waits to be told it is banned
    is therefore never told, and every retry it makes in the meantime is more
    traffic into the thing doing the banning. Absence of traffic has to be a
    first-class ban signal, and the retry budget has to answer to it.
    """

    def test_a_silent_ban_carries_no_response_code(self, server, make_link):
        """The invariant SPEC-002 states: a silent ban has no code, because nothing replied.

        Both constructors are checked here rather than each of the four call
        sites, because this is where the code would leak in. The handshake path
        is the interesting one: it is *handed* "604" on purpose, since only a
        response code can tell the table that an unanswered handshake is
        retryable rather than a latched credential refusal. That code is a local
        stand-in for a reply that never came, and it must not reach the caller as
        though AniDB had sent it -- `rescode` means what AniDB answered with.
        """
        link = make_link(server)

        refusal = link.set_banned(reason=b"AniDB stopped answering", cause=BanCause.SILENCE)
        assert refusal.cause is BanCause.SILENCE
        assert refusal.rescode is None

        error = link.auth_failed("604", "API not responding", cause=BanCause.SILENCE)
        assert isinstance(error, AniDBBannedError)
        assert error.cause is BanCause.SILENCE
        assert error.rescode is None, "604 classifies the failure; it is not a code AniDB sent"

    def test_one_unanswered_command_is_not_silence(self, server, make_link):
        """UDP loses datagrams. Absorbing one is what the retry budget is for."""
        link = make_link(server)

        assert link._listener._note_silent_timeout(anidb_client.commands.PingCommand()) is False
        assert not link.is_banned

    def test_the_gate_closes_at_the_threshold(self, server, make_link):
        link = make_link(server)
        listener = link._listener

        verdicts = [
            listener._note_silent_timeout(anidb_client.commands.PingCommand())
            for _ in range(listener.SILENT_TIMEOUTS_BEFORE_BAN)
        ]

        assert verdicts[:-1] == [False] * (listener.SILENT_TIMEOUTS_BEFORE_BAN - 1)
        assert verdicts[-1] is True

    def test_a_datagram_of_any_kind_resets_the_count(self, server, make_link):
        """Only a consecutive run counts, and anything arriving ends the run.

        Any datagram: a reply to some other command, an untagged notice, even a
        packet this client cannot parse. All three say the API is still there.
        """
        link = make_link(server)
        listener = link._listener

        for _ in range(listener.SILENT_TIMEOUTS_BEFORE_BAN - 1):
            listener._note_silent_timeout(anidb_client.commands.PingCommand())
        listener._note_datagram()

        assert listener._note_silent_timeout(anidb_client.commands.PingCommand()) is False

    def test_a_handshake_timeout_is_not_counted(self, server, make_link):
        """A handshake never retries: it backs off on its first unanswered attempt.

        So it needs no detector, and feeding it into one would register a single
        silence as two separate bans and double the back-off for it.
        """
        link = make_link(server)
        listener = link._listener
        auth = anidb_client.commands.AuthCommand("user", "pw", "3", "anidbclientpy", 1, nat=1)

        for _ in range(listener.SILENT_TIMEOUTS_BEFORE_BAN * 2):
            assert listener._note_silent_timeout(auth) is False

    def test_a_command_that_timed_out_while_replies_arrived_is_not_silence(self, server, make_link):
        """The sweep's first branch already covers this, and must keep covering it.

        A command older than the last readable reply timed out while the API was
        demonstrably answering -- a re-authentication in flight, most likely. It
        is put back rather than counted, and counting it would let ordinary
        session recovery close the gate on itself.
        """
        link = make_link(server)
        listener = link._listener
        command = anidb_client.commands.PingCommand()
        command.tag = "T700"
        command.callback = lambda _resp: None
        command.started = monotonic() - listener.timeout - 1
        listener.queue_command(command)
        # A reply landed after that command went out.
        listener._last_receive = monotonic()

        listener._handle_timeouts()

        assert listener._silent_timeouts == 0, "a command timing out against a live API was read as silence"
        assert not link.is_banned

    def test_silence_closes_the_gate_end_to_end(self, server, make_link):
        """The whole thing, through the real transport: AniDB simply stops answering."""
        server.on("PING", lambda req: None)
        link = make_link(server)

        future = link.request(anidb_client.commands.PingCommand(), lambda _resp: None)
        with contextlib.suppress(AniDBBannedError):
            future.result(timeout=20)

        assert link.is_banned, "total silence never closed the gate"
        assert link.ban_cause is BanCause.SILENCE
        assert link.ban_remaining > 0
        assert link.reported_address is None, "nothing was ever reported back to read"

    def test_the_gate_stops_the_retries(self, server, make_link):
        """The point of detecting it: stop sending.

        Every attempt after the gate closes is a datagram into a service whose
        way of saying no is to ignore them, so the command fails where it would
        otherwise have been re-sent.
        """
        server.on("UPTIME", lambda req: None)
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        future = link.request(anidb_client.commands.UptimeCommand(), lambda _resp: None)
        with pytest.raises(AniDBBannedError):
            future.result(timeout=20)
        threading.Event().wait(0.5)

        sent = len(server.requests_for("UPTIME"))
        assert sent == AniDBListener.SILENT_TIMEOUTS_BEFORE_BAN, f"UPTIME reached AniDB {sent} times"


class TestHealthSurface:
    """What an embedder can read without reaching into private state or sending.

    An application holding this transport has to be able to tell "quiet because
    there is nothing to do" from "quiet because AniDB has closed the gate", and
    the only ways to find out were to touch private attributes or to send a
    command -- which, during a ban, is precisely the thing not to do.
    """

    def test_a_fresh_link_reports_no_ban(self, server, make_link):
        link = make_link(server)

        assert link.is_banned is False
        assert link.ban_cause is None
        assert link.ban_remaining == 0
        assert link.ban_multiplier == 0
        assert link.session_age is None

    def test_a_ban_is_visible_with_its_cause_and_its_remaining_time(self, server, make_link):
        server.on("AUTH", lambda req: b"555 BANNED\n")
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link.is_banned, message="the ban was never registered")
        assert link.ban_cause is BanCause.REFUSED
        assert link.ban_multiplier == 1
        # Unrounded, in seconds: the message this used to be rounded to whole
        # minutes, so anything under thirty seconds read as "0 minutes".
        assert 0 < link.ban_remaining <= RateLimiter.BAN_BASE_DELAY

    def test_the_address_anidb_reported_is_kept(self, server, make_link):
        """The only outside confirmation that source-port pinning is working.

        AniDB echoes the address it saw this client arrive from, and the
        transport parsed it, compared the port, and threw the whole thing away.
        The IP matters as much as the port: together they are what AniDB counts
        requests against.
        """
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._authed.is_set(), message="never authenticated")
        assert link.reported_address == ("127.0.0.1", 9000)

    def test_no_address_is_reported_when_the_reply_carries_none(self, server, make_link):
        """AniDB returns it only when AUTH asked for it, so its absence is normal."""
        server.on("AUTH", "200 sess1234 LOGIN ACCEPTED")
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._authed.is_set(), message="never authenticated")
        assert link.reported_address is None

    def test_session_age_starts_at_the_login_and_ends_with_the_session(self, server, make_link):
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._authed.is_set(), message="never authenticated")
        age = link.session_age
        assert age is not None
        assert 0 <= age < 5

        link.set_session(None)
        assert link.session_age is None


class TestFailedAuthentication:
    """A handshake AniDB refuses must settle, not park the sender.

    The reported hang: `504 CLIENT BANNED` answers AUTH, so it arrives tagged and
    reached the success handler, whose first act was to read a field only a
    successful reply carries. That raised KeyError on a response thread -- where
    nothing sees it -- leaving the authenticated event unset and the
    authenticating flag set, so every later attempt short-circuited and the sender
    waited forever on a reply that had already been delivered and dropped.

    Every refusal code AniDB can answer a handshake with lands in the same place,
    which is why these are parametrized rather than written for 504 alone. A wrong
    password was as permanent a hang as a ban.
    """

    @pytest.mark.parametrize(
        ("code", "text"),
        [
            ("500", "LOGIN FAILED"),
            ("502", "ACCESS DENIED"),
            ("503", "CLIENT VERSION OUTDATED"),
            ("504", "CLIENT BANNED"),
        ],
    )
    def test_a_refused_handshake_settles_instead_of_hanging(self, server, make_link, code, text):
        server.on("AUTH", f"{code} {text}")
        link = make_link(server)
        link.reauthenticate()

        _await(
            lambda: not link._authenticating.is_set(),
            message=f"{code} left the handshake latched; every later attempt would short-circuit",
        )
        assert not link._authed.is_set()
        assert link._listener.is_alive(), f"{code} killed the listener"

    @pytest.mark.parametrize(
        ("code", "text"),
        [
            ("500", "LOGIN FAILED"),
            ("502", "ACCESS DENIED"),
            ("503", "CLIENT VERSION OUTDATED"),
            ("504", "CLIENT BANNED"),
        ],
    )
    def test_a_refused_handshake_carries_the_code_it_was_refused_with(self, server, make_link, code, text):
        """A refusal that needs a human still has to say which human, doing what.

        "The login failed" and "this client version is no longer registered" are
        both latched and both need someone to act, but not the same someone: one
        is a credential to correct, the other a registration to renew. A caller
        that can only read the message string cannot route them differently, so
        the code AniDB refused with is carried as data (SPEC-002).
        """
        server.on("AUTH", f"{code} {text}")
        link = make_link(server)
        link.reauthenticate()

        _await(
            lambda: not link._authenticating.is_set(),
            message=f"{code} never settled, so there is no refusal to read",
        )
        attempt = link._auth_attempt
        assert attempt is not None and attempt.done()
        error = attempt.exception()
        assert error is not None, f"{code} settled as a success"
        assert getattr(error, "rescode", None) == code

    def test_a_rejected_credential_is_not_offered_again(self, server, make_link):
        """The gentleness rule: retrying a refusal is how a refusal becomes a ban.

        AniDB has said these credentials are wrong. Nothing about sending them
        again changes that, so the transport latches the failure and stops.
        """
        server.on("AUTH", "500 LOGIN FAILED")
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._auth_fatal is not None, message="a fatal refusal was not recorded")

        # Every route back to the wire, refused.
        link.reauthenticate()
        link._reauthenticate()
        threading.Event().wait(0.3)

        sent = len(server.requests_for("AUTH"))
        assert sent == 1, f"credentials AniDB already rejected were sent {sent} times"

    def test_a_ban_is_retryable_and_backs_off(self, server, make_link):
        """504 is not a wrong password: it clears on its own, so it may be retried.

        What must not happen is retrying it immediately, which is what earns the
        next one.
        """
        server.on("AUTH", "504 CLIENT BANNED")
        link = make_link(server)
        link.reauthenticate()

        _await(lambda: link._rate_limiter.is_banned, message="a banned handshake registered no back-off")
        assert link._auth_fatal is None, "a ban is temporary and must not latch"

    def test_a_handshake_that_is_never_answered_settles_too(self, server, make_link):
        """Silence has to end the attempt as surely as a refusal does.

        The AUTH command's timeout used to back off without settling anything, so
        an unanswered handshake parked the sender exactly like a refused one.
        """
        server.on("AUTH", lambda req: None)
        link = make_link(server)
        link.reauthenticate()

        _await(
            lambda: not link._authenticating.is_set(),
            timeout=8.0,
            message="an unanswered handshake never settled",
        )

    def test_an_unanswered_encryption_handshake_settles_too(self, server, make_link):
        """The handshake has two legs when a key is configured, and either can stall.

        ENCRYPT goes first and AUTH never follows it, so a silent ENCRYPT parks
        the sender at exactly the same place a silent AUTH does. It gets the same
        treatment for the same reason, and this pins the leg that is easy to
        forget: without a key configured, nothing sends ENCRYPT at all.
        """
        server.on("ENCRYPT", lambda req: None)
        link = make_link(server, api_key="0123456789abcdef")
        link.reauthenticate()

        server.wait_for("ENCRYPT")
        _await(
            lambda: not link._authenticating.is_set(),
            timeout=8.0,
            message="an unanswered encryption handshake never settled",
        )
        assert not link._authed.is_set()


class TestCommandOutcome:
    """Every request settles, and says which way it went.

    A callback can only report success: there is no callback for a reply that
    never arrives. So a caller could not tell "AniDB has no such anime" from "we
    were banned and gave up", and on the second it waited on a signal nothing was
    going to send.
    """

    def test_a_reply_settles_the_command_that_asked_for_it(self, server, make_link):
        server.on("PING", "300 PONG")
        link = make_link(server)

        future = link.request(anidb_client.commands.PingCommand(), lambda _resp: None)

        assert future.result(timeout=5).rescode == "300"

    def test_the_callback_has_already_run_when_the_outcome_arrives(self, server, make_link):
        """Ordering, not decoration.

        The callback is what writes the reply into the cache. A caller released
        before that has finished would read the cache and find the answer it was
        just told had arrived missing from it.
        """
        server.on("PING", "300 PONG")
        link = make_link(server)
        handled = threading.Event()

        def slow_callback(_resp):
            time.sleep(0.2)
            handled.set()

        future = link.request(anidb_client.commands.PingCommand(), slow_callback)
        future.result(timeout=5)

        assert handled.is_set(), "the outcome arrived before the callback finished"

    def test_a_command_that_is_never_answered_fails_its_caller(self, server, make_link):
        """The reported hang, reduced to one command.

        AniDB stops answering. The caller is told -- rather than waiting on
        `_updated.wait()` for the life of the process.

        What it is told is the silence gate, not a bare timeout: by the time the
        transport gives up it has concluded AniDB is not answering this client at
        all, and that is a more useful thing to hand back than "this one command
        did not come back". The reason carries how long the window has to run, so
        the caller can decide when to return.
        """
        server.on("PING", lambda req: None)
        link = make_link(server)

        future = link.request(anidb_client.commands.PingCommand(), lambda _resp: None)

        with pytest.raises(AniDBBannedError) as raised:
            future.result(timeout=20)
        assert raised.value.cause is BanCause.SILENCE
        assert raised.value.retry_after > 0
        assert raised.value.rescode is None, "nothing answered, so there is no code AniDB gave"

    def test_an_unanswered_command_reaches_the_wire_a_bounded_number_of_times(self, server, make_link):
        """What the API sees while all that is going on.

        A service that bans clients for asking too often counts requests, so the
        bound that matters is this one. It used to be unbounded: the budget was
        restored every time it ran out.

        Against total silence the bound is the silence threshold rather than the
        attempt budget, and it is the tighter of the two on purpose. The budget
        is sized for a lost datagram; silence is AniDB's documented way of
        enforcing a ban, and spending the rest of the budget on it is firing more
        traffic into the thing doing the banning.
        """
        server.on("PING", lambda req: None)
        link = make_link(server)

        future = link.request(anidb_client.commands.PingCommand(), lambda _resp: None)
        with contextlib.suppress(AniDBBannedError):
            future.result(timeout=20)
        threading.Event().wait(0.5)

        sent = len(server.requests_for("PING"))
        assert sent == AniDBListener.SILENT_TIMEOUTS_BEFORE_BAN, f"PING reached AniDB {sent} times"
        assert sent < anidb_client.commands.Command.MAX_ATTEMPTS, "the gate did not cut the retries short"

    def test_a_callback_that_raises_fails_the_command(self, server, make_link):
        """A reply that arrives and is then mishandled is still no answer.

        The handler ran on a thread nobody joins, so an exception in it was
        invisible at the time and left the caller waiting on a reply that had in
        fact arrived.
        """
        server.on("PING", "300 PONG")
        link = make_link(server)

        def broken(_resp):
            raise ValueError("callback is broken")

        future = link.request(anidb_client.commands.PingCommand(), broken)

        with pytest.raises(ValueError, match="callback is broken"):
            future.result(timeout=5)


class TestBackingOffWithoutSleeping:
    """A back-off is a window during which nothing is sent, not a sleep.

    It used to be a sleep taken in the send path. The listener thread reaches that
    path -- through an untagged ban notice, and through its own timeout sweep --
    so the thread whose only job is to read the socket could spend up to four
    hours not reading it. Every reply arriving in that window was lost, and no
    command could time out either, because the sweep runs on that same thread.

    These use a real sleeping limiter rather than the fixture's no-op one: a test
    that cannot tell sleeping from not sleeping cannot check this.
    """

    def test_the_listener_keeps_reading_while_the_back_off_runs(self, server, make_link):
        server.on("AUTH", lambda req: b"555 BANNED\n")
        link = make_link(server, rate_limiter=RateLimiter())
        link.reauthenticate()

        _await(lambda: link._rate_limiter.is_banned, message="the ban was never registered")

        # The listener answering a ping is proof it is still in its receive loop.
        threading.Event().wait(0.3)
        assert link._listener.is_alive(), "the listener died"
        assert link._listener.sock is not None, "the listener stopped reading the socket"

    def test_nothing_is_sent_while_the_back_off_is_open(self, server, make_link):
        """The gentleness rule, and the whole reason to fail rather than wait.

        The sleeping version still intended to send, and did, the moment the clock
        allowed it. This one sends nothing -- not the command, and not the
        authentication the command would have triggered.
        """
        server.on("AUTH", lambda req: b"555 BANNED\n")
        link = make_link(server, rate_limiter=RateLimiter())
        link.reauthenticate()
        _await(lambda: link._rate_limiter.is_banned, message="the ban was never registered")

        sent_before = len(server.received_commands())
        # UPTIME rather than PING: PING is one of the few commands dispatched
        # without a session, so it would not exercise the queue the rest go through.
        futures = [link.request(anidb_client.commands.UptimeCommand(), lambda _resp: None) for _ in range(3)]
        threading.Event().wait(0.5)

        assert len(server.received_commands()) == sent_before, "commands went out during a back-off"
        for future in futures:
            with pytest.raises(AniDBBannedError):
                future.result(timeout=5)

    def test_the_caller_is_told_rather_than_held(self, server, make_link):
        """Half an hour of silence is right; half an hour of blocking is not.

        The reported incident is precisely this distinction: the process was doing
        the correct thing to AniDB and the wrong thing to its caller.
        """
        server.on("AUTH", lambda req: b"555 BANNED\n")
        link = make_link(server, rate_limiter=RateLimiter())
        link.reauthenticate()
        _await(lambda: link._rate_limiter.is_banned, message="the ban was never registered")

        started = monotonic()
        with pytest.raises(AniDBBannedError):
            link.request(anidb_client.commands.UptimeCommand(), lambda _resp: None).result(timeout=10)

        assert monotonic() - started < 5, "the caller was held for the back-off instead of being told"


class TestListenerRobustness:
    def test_an_unparsable_reply_does_not_kill_the_listener(self, server, make_link):
        """Regression: this path used to call sys.exit(2).

        A library must not terminate its host process, and in a non-main thread
        sys.exit only ends that thread -- so the listener died silently and every
        subsequent command timed out with no explanation. The listener must
        survive garbage and keep serving.
        """
        server.on("AUTH", lambda req: b"\xff\xfe not a valid reply at all\n")
        link = make_link(server)
        link.reauthenticate()
        server.wait_for("AUTH")

        threading.Event().wait(0.3)
        assert link._listener.is_alive(), "listener thread died on a malformed packet"

        # The unanswered handshake settles rather than latching: an attempt that
        # ends without releasing its waiter is the hang, not a lost result.
        _await(
            lambda: not link._authenticating.is_set(),
            message="the handshake never settled, so nothing could start another",
        )

        # And it still works afterwards. Two things stand between here and that:
        # a fresh handshake is only started when something asks for one, and the
        # unanswered one registered a back-off during which nothing is sent at all.
        # Clearing it stands in for the window elapsing -- the point of this test
        # is the listener, and the back-off has its own.
        link._rate_limiter.clear_ban()
        server.on("AUTH", AUTH_OK)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="listener stopped serving after garbage")

    def test_an_unrecognised_untagged_code_does_not_kill_the_listener(self, server, make_link):
        """The other former sys.exit(2): a valid reply with an unhandled code."""
        server.on("AUTH", lambda req: b"799 SOMETHING UNDOCUMENTED\n")
        link = make_link(server)
        link.reauthenticate()
        server.wait_for("AUTH")

        threading.Event().wait(0.3)
        assert link._listener.is_alive()

    def test_the_timeout_sweep_survives_a_command_re_queued_while_it_ran(self, server, make_link, monkeypatch):
        """`started` is None between a re-queue and the next send.

        The sweep runs in two passes -- collect the expired tags, then pop each
        one -- and request() sets `started` back to None. A re-queue landing in
        that window, from the sender thread or an application thread, leaves the
        second pass comparing None to a float. That raised TypeError on the
        listener thread, which ends the listener; nothing then reads the socket
        and every caller waits on a reply that can no longer arrive.

        Forced here by clearing `started` inside pop_command, which is precisely
        the interleaving: expired at collection time, re-queued by the time it is
        claimed. A command that has not been sent has not timed out either, so it
        belongs in the same branch as one that started before the last reply --
        put back, not handed to handle_timeout.
        """
        link = make_link(server)
        listener = link._listener
        command = anidb_client.commands.PingCommand()
        command.tag = "T700"
        command.callback = lambda _resp: None
        command.started = monotonic() - listener.timeout - 1
        listener.queue_command(command)

        real_pop = listener.pop_command

        def pop_and_requeue(tag):
            claimed = real_pop(tag)
            if claimed is not None:
                claimed.started = None
            return claimed

        monkeypatch.setattr(listener, "pop_command", pop_and_requeue)

        listener._handle_timeouts()

        assert listener.is_alive(), "listener died sweeping a command re-queued mid-sweep"
        assert command in [cmd for _tag, cmd in listener.pending_commands()], "the re-queued command was dropped"


class TestTagAllocation:
    def test_tags_are_sequential_and_zero_padded(self, server, make_link):
        link = make_link(server)
        assert [link._new_tag() for _ in range(3)] == ["T001", "T002", "T003"]

    def test_tags_roll_over_to_t000(self, server, make_link):
        """Regression: the rollover value was the string "TOOO" -- letters."""
        link = make_link(server)
        link._current_tag = 999
        assert link._new_tag() == "T000"
        assert link._new_tag() == "T001"

    def test_concurrent_callers_never_get_the_same_tag(self, server, make_link):
        """request() is called from the sender thread, from the listener thread
        (on a lost session, and on a timeout re-queue) and from whichever
        application thread asked for data. Two of them reading and incrementing
        the counter unguarded can hand out one tag twice, which crosses one
        reply onto the other's command -- and the command that loses never gets
        an answer, so whoever is waiting on it waits forever.
        """
        link = make_link(server)
        tags = []
        lock = threading.Lock()
        start = threading.Event()

        def take():
            start.wait()
            mine = [link._new_tag() for _ in range(100)]
            with lock:
                tags.extend(mine)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()

        assert len(tags) == len(set(tags)), "a tag was issued twice"


class TestCommandQueue:
    """The queue that ties a reply back to the command that asked for it.

    It is written by the sender thread and read, popped and iterated by the
    listener. Iterating a dict another thread is inserting into raises
    RuntimeError, and a RuntimeError raised in the listener ends it -- after
    which nothing reads the socket and every caller waits on an event that can
    no longer be set.
    """

    def test_a_queued_command_can_be_claimed_once(self, server, make_link):
        link = make_link(server)
        listener = link._listener
        command = anidb_client.commands.PingCommand()
        command.tag = "T500"
        listener.queue_command(command)

        assert listener.pop_command("T500") is command
        assert listener.pop_command("T500") is None, "claiming twice must not raise"

    def test_claiming_an_unknown_tag_answers_none(self, server, make_link):
        """A reply for a command already timed out and swept. Common, not an error."""
        link = make_link(server)

        assert link._listener.pop_command("T999") is None

    def test_iterating_while_another_thread_queues_does_not_raise(self, server, make_link):
        """The RuntimeError this lock exists to prevent."""
        link = make_link(server)
        listener = link._listener
        stop = threading.Event()
        errors = []

        def writer():
            counter = 0
            while not stop.is_set():
                command = anidb_client.commands.PingCommand()
                command.tag = f"W{counter:04d}"
                listener.queue_command(command)
                listener.pop_command(command.tag)
                counter += 1

        def reader():
            try:
                while not stop.is_set():
                    for _tag, cmd in listener.pending_commands():
                        _ = cmd.command
            except Exception as exc:  # noqa: BLE001 - the failure is what is under test
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        threading.Event().wait(0.5)
        stop.set()
        for thread in threads:
            thread.join()

        assert errors == []

        # Drain whatever the writer left behind. Otherwise the listener's timeout
        # sweep re-sends it during teardown, against a socket that is closing.
        for tag, _cmd in listener.pending_commands():
            listener.pop_command(tag)


class TestDecryptionOfUnencryptedPackets:
    """AniDB sends some replies in the clear on an encrypted session.

    An untagged ban notice is the case that matters: it arrives before the
    cipher is established, and it is what the client most needs to be able to
    read. run() therefore decrypts speculatively and suppresses ValueError,
    falling back to reading the packet as plaintext.

    So the padding check has to raise ValueError specifically. Any other type
    escapes that suppression and ends the listener thread, which is the
    permanent hang this whole exercise is about.
    """

    def test_a_plaintext_packet_of_block_length_is_rejected_as_unencrypted(self, server, make_link):
        """16 bytes of plaintext used to decrypt to noise, get truncated by
        whatever its last byte happened to say, and be parsed as a reply."""
        from Crypto.Cipher import AES

        link = make_link(server)
        listener = link._listener
        listener.cipher = AES.new(b"0123456789abcdef", AES.MODE_ECB)

        with pytest.raises(ValueError):
            listener.decrypt(b"555 BANNED\n" + b" " * 5)

    def test_an_empty_packet_is_rejected_as_unencrypted(self, server, make_link):
        from Crypto.Cipher import AES

        link = make_link(server)
        link._listener.cipher = AES.new(b"0123456789abcdef", AES.MODE_ECB)

        with pytest.raises(ValueError):
            link._listener.decrypt(b"")

    def test_a_properly_padded_packet_round_trips(self, server, make_link):
        """The check must not reject real traffic."""
        from Crypto.Cipher import AES

        link = make_link(server)
        listener = link._listener
        cipher = AES.new(b"0123456789abcdef", AES.MODE_ECB)
        listener.cipher = cipher

        packet = listener.encrypt(b"200 sess1234 LOGIN ACCEPTED", cipher)

        assert listener.decrypt(packet) == b"200 sess1234 LOGIN ACCEPTED"


class TestSharedTransportState:
    """The session key and the cipher are written by the listener thread and read
    by the sender thread on every command.

    Both now sit behind accessors that take a lock, so a command cannot be
    authorized with a session that was cleared between the check and the use, and
    the pair cannot be observed half-cleared.
    """

    def test_the_session_reads_back_through_the_accessor(self, server, make_link):
        link = make_link(server)
        link.set_session("abc123")

        assert link.session == "abc123"

    def test_reauthenticate_clears_session_and_cipher_together(self, server, make_link, monkeypatch):
        """A half-cleared state is a command encrypted with a key the server has
        forgotten, or an unencrypted command on a session that requires one.

        `_reauthenticate` is stubbed out so this tests the clearing alone rather
        than the round trip that follows it.
        """
        from Crypto.Cipher import AES

        link = make_link(server)
        link.set_session("abc123")
        link._listener.cipher = AES.new(b"0123456789abcdef", AES.MODE_ECB)
        monkeypatch.setattr(link, "_reauthenticate", lambda: None)

        link.reauthenticate()

        assert link.session is None
        assert link._listener.cipher is None

    def test_only_one_thread_can_settle_a_handshake(self, server, make_link):
        """Two threads race to settle the same attempt; exactly one may win.

        Both are real: the listener settles on a reply, and its own timeout sweep
        settles on silence. A reply landing as the sweep claims the same command
        puts them on the same attempt at the same moment. Settling twice raises
        InvalidStateError out of whichever lost -- on the listener thread, where
        that ends the listener and hangs every caller.

        The check and the settle both happen inside the lock, so the loser sees an
        attempt that is already done and leaves it alone. The settled attempt stays
        in place: a waiter arriving afterwards has to be able to read why it ended.
        """
        link = make_link(server)
        with link._auth_lock:
            link._auth_attempt = Future()
            attempt = link._auth_attempt

        start = threading.Barrier(2)
        errors = []

        def settle(error):
            start.wait()
            try:
                link._settle_auth(error)
            except Exception as e:  # pragma: no cover - the assertion below reports it
                errors.append(e)

        threads = [
            threading.Thread(target=settle, args=(None,)),
            threading.Thread(target=settle, args=(AniDBBannedError("banned"),)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors, f"settling raced: {errors}"
        assert attempt.done(), "the attempt was left unsettled"
        # Readable afterwards, and settled exactly once: a second settle would have
        # raised, and the recorded outcome is whichever call won rather than a mix.
        assert link._auth_attempt is attempt
        link._settle_auth(None)

    def test_settling_an_attempt_that_is_already_gone_is_harmless(self, server, make_link):
        """The sweep can reach a handshake that settled a moment earlier.

        Nothing to release is not an error; raising here would end the thread that
        found out.
        """
        link = make_link(server)

        link._settle_auth(None)
        link._settle_auth(AniDBBannedError("banned"))

    def test_a_latched_failure_stops_a_new_attempt_from_starting(self, server, make_link):
        """The latch is read under the lock before any attempt begins.

        Read outside it, the window between "not fatal" and "start sending" is
        one where a refusal recorded by the listener is missed and the credentials
        AniDB just rejected go out again.
        """
        link = make_link(server)
        link._auth_fatal = AniDBAuthFailedError("refused")

        link._reauthenticate()

        assert link._auth_attempt is None, "a fatal refusal must not start another handshake"
        assert not link._authenticating.is_set()
        assert server.requests_for("AUTH") == []

    def test_decrypting_with_no_cipher_is_a_value_error(self, server, make_link):
        """ValueError specifically. run() suppresses exactly that and falls back to
        reading the packet as plaintext -- which is what a packet arriving after the
        session dropped actually is. Any other type ends the listener thread, and a
        listener that has stopped reading is a permanent hang for every caller.
        """
        link = make_link(server)

        with pytest.raises(ValueError):
            link._listener.decrypt(b"555 BANNED\n")

    def test_encrypt_uses_the_cipher_it_is_handed(self, server, make_link):
        """It takes the cipher as an argument rather than reading it back, so the
        caller's `if cipher` test and this use are guaranteed to be the same read.
        There is no vanished-cipher case on the send path to handle."""
        from Crypto.Cipher import AES

        link = make_link(server)
        cipher = AES.new(b"0123456789abcdef", AES.MODE_ECB)

        encrypted = link._listener.encrypt(b"PING\n", cipher)

        assert link._listener.cipher is None, "encrypt must not depend on the stored cipher"
        assert len(encrypted) % 16 == 0


class TestListenerStartup:
    def test_the_listener_does_not_start_itself(self, monkeypatch):
        """It used to call self.start() from its own constructor.

        The listener reaches back into AniDBLink -- set_banned, reauthenticate,
        request -- and AniDBLink builds it partway through its own __init__, before
        the auth lock, the events and the session attribute exist. A reply arriving
        in that window hit an AttributeError on the listener thread and killed it,
        after which nothing read the socket and every caller waited on a reply that
        could no longer arrive. AniDBLink now starts it once it is fully built.
        """
        from anidb_client.link import AniDBListener

        # Built without make_link, which is what normally installs the module
        # logger; stop() logs on the way out.
        monkeypatch.setattr(anidb_client, "log", logging.getLogger("anidb_client.test"), raising=False)

        listener = AniDBListener(sender=None, myport=0, timeout=1)
        try:
            assert not listener.is_alive()
        finally:
            listener.stop()

    def test_a_link_starts_its_listener(self, server, make_link):
        link = make_link(server)

        assert link._listener.is_alive()


class TestSenderWakeup:
    def test_a_queued_command_is_sent_without_waiting_for_a_poll(self, server, make_link):
        """The sender waits to be woken rather than polling an empty queue.

        Measured across several round trips, each of which leaves the sender idle
        again before the next is queued. A polling sender costs up to IDLE_TICK per
        command -- so this many of them would take on the order of a second and a
        half. Being woken costs nothing, and what is left is the harness's own
        20ms poll in `_await`.

        UPTIME rather than PING: PING is sent immediately by request() and never
        reaches the queue this test is about.
        """
        server.on("AUTH", AUTH_OK)
        server.on("UPTIME", "208 34400")
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        rounds = 15
        started = time.monotonic()
        for _ in range(rounds):
            got = []
            link.request(anidb_client.commands.UptimeCommand(), got.append)
            # Bound explicitly: `got` is rebound each iteration, and a bare closure
            # over it is what B023 warns about even though this one is consumed
            # before the next pass.
            _await(lambda pending=got: pending, message="UPTIME callback never fired")
        elapsed = time.monotonic() - started

        budget = rounds * AniDBLink.IDLE_TICK / 3
        assert elapsed < budget, f"{rounds} round trips took {elapsed:.2f}s; a polling sender is the likely cause"


class TestCallbackIsolation:
    def test_a_slow_callback_does_not_stall_the_next_reply(self, server, make_link):
        """Each reply's callback runs on its own thread.

        If they ran on the receive loop, one callback that blocks would stop every
        later reply from being read at all -- the same permanent hang as a dead
        listener, arrived at from the other direction.
        """
        server.on("AUTH", AUTH_OK)
        server.on("PING", "300 PONG")
        link = make_link(server)
        link.reauthenticate()
        _await(lambda: link._authed.is_set(), message="never authenticated")

        blocked = threading.Event()
        release = threading.Event()
        second = []

        def slow(_resp):
            blocked.set()
            release.wait(5)

        link.request(anidb_client.commands.PingCommand(), slow)
        _await(lambda: blocked.is_set(), message="the first callback never ran")

        try:
            link.request(anidb_client.commands.PingCommand(), second.append)
            _await(lambda: second, message="a blocked callback stalled the receive loop")
        finally:
            release.set()


class TestStoppingTheTransport:
    """`stop()` ends the transport and gives the source port back.

    There was no path through it that did both. Authenticated, it sent LOGOUT,
    waited for the acknowledgement and returned -- never closing the socket, so a
    clean `init()`/`close()` cycle left the pinned port bound for the life of the
    process and the listener thread reading it. Unauthenticated, it closed the
    socket but never signalled the sender, which went on waking every idle tick
    forever. Each branch did half the job, and neither did the other half.

    The port matters more than it looks. Releasing it is not simply a matter of
    closing the descriptor: a thread blocked in `recv` keeps the socket alive in
    the kernel until that call returns, so the port stays bound after the close.
    With an ephemeral, shareable port that is invisible. With one pinned port and
    no SO_REUSEADDR (ADR-007) it is the difference between a client that can
    restart and one that cannot.
    """

    def test_the_port_is_free_once_stop_returns(self, server, make_link):
        """The property a restarting or retrying caller actually depends on."""
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("", 0))
        port = holder.getsockname()[1]
        holder.close()

        link = make_link(server, myport=port)
        link.stop()

        # Rebound immediately, in this process, with no waiting. This failed with
        # EADDRINUSE while the listener was still sitting in recv.
        rebound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rebound.bind(("", port))
        finally:
            rebound.close()

    def test_the_port_is_free_after_an_authenticated_stop(self, server, make_link):
        """The branch that logs out has to release it too, and released nothing."""
        server.on("AUTH", AUTH_OK)
        server.on("LOGOUT", "203 LOGGED OUT")

        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("", 0))
        port = holder.getsockname()[1]
        holder.close()

        link = make_link(server, myport=port)
        link.request(anidb_client.commands.UptimeCommand(), lambda resp: None)
        server.wait_for("UPTIME")

        link.stop()

        rebound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rebound.bind(("", port))
        finally:
            rebound.close()

    def test_both_threads_end(self, server, make_link):
        """A stopped transport leaves nothing running, which is the stated property."""
        link = make_link(server)
        link.stop()

        assert not link.is_alive(), "the sender thread is still running"
        assert not link._listener.is_alive(), "the listener thread is still running"

    def test_an_unauthenticated_stop_sends_nothing(self, server, make_link):
        """A courtesy packet from a client with no session is worse than silence."""
        link = make_link(server)
        link.stop()

        assert server.received_commands() == []

    def test_logout_false_sends_nothing_even_when_authenticated(self, server, make_link):
        """The teardown a failed `init()` uses: end it, do not talk to it.

        A half-built client may be one that is backing off, and while a back-off is
        open sending is the one thing that must not happen (SPEC-002).
        """
        server.on("AUTH", AUTH_OK)
        server.on("LOGOUT", "203 LOGGED OUT")

        link = make_link(server)
        link.request(anidb_client.commands.UptimeCommand(), lambda resp: None)
        server.wait_for("UPTIME")

        link.stop(logout=False)

        assert "LOGOUT" not in server.received_commands()

    def test_a_logout_that_is_never_answered_still_tears_down(self, server, make_link):
        """The case that hurt an embedder most: a banned client cannot be told.

        `stop()` waited the full command timeout for an acknowledgement that was
        never coming and *then* returned with everything still running. Now the
        wait is bounded and the teardown happens either way.
        """
        server.on("AUTH", AUTH_OK)
        server.on("LOGOUT", lambda req: None)

        link = make_link(server)
        link.request(anidb_client.commands.UptimeCommand(), lambda resp: None)
        server.wait_for("UPTIME")

        started = monotonic()
        link.stop(timeout=0.5)
        elapsed = monotonic() - started

        assert link._listener.sock is None
        assert not link._listener.is_alive()
        # Bounded by what was asked for, not by the command timeout.
        assert elapsed < 5, f"stop() took {elapsed:.1f}s despite being given 0.5s"

    def test_stopping_twice_is_safe(self, server, make_link):
        """`close()` may be called on a client that has already been closed."""
        link = make_link(server)
        link.stop()
        link.stop()

    def test_a_command_in_flight_is_failed_rather_than_stranded(self, server, make_link):
        """SPEC-002: a waiter is always released.

        A command outstanding when the transport stops can never be answered, and
        leaving it registered hangs whoever asked for it instead of telling them.
        """
        server.on("AUTH", AUTH_OK)
        server.on("UPTIME", lambda req: None)

        link = make_link(server)
        command = anidb_client.commands.UptimeCommand()
        link.request(command, lambda resp: None)
        server.wait_for("UPTIME")

        link.stop(logout=False)

        with pytest.raises(AniDBError):
            command.future.result(timeout=5)


class TestConnectingOnDemand:
    """`connect()` establishes the session at startup instead of on first use.

    The transport authenticates lazily, so a wrong password or a standing ban is
    otherwise discovered by whichever request happens to be first. For a service
    that is an hour after boot, in front of a user. This is the call that asks the
    question early -- and the properties that matter are that it is idempotent and
    that it reports the reason, because a startup check that silently succeeded or
    that logged in twice per call would be worse than not having one.
    """

    def test_it_establishes_a_session(self, server, make_link):
        server.on("AUTH", AUTH_OK)

        link = make_link(server)
        link.connect()

        assert link.session == "sess1234"
        assert len(server.requests_for("AUTH")) == 1

    def test_calling_it_again_does_not_authenticate_again(self, server, make_link):
        """The property that keeps it from being a way to get banned.

        Against an API that meters by request frequency, a call shaped like this
        one is exactly what someone wires into a readiness probe. A second login
        per invocation would turn that habit into an IP ban.
        """
        server.on("AUTH", AUTH_OK)

        link = make_link(server)
        link.connect()
        link.connect()
        link.connect()

        assert len(server.requests_for("AUTH")) == 1

    def test_concurrent_callers_produce_one_handshake(self, server, make_link):
        """The third case the idempotence rests on: an attempt already in flight.

        A session already up and a credential already refused are the easy two.
        This is the one that only appears under load -- two threads reaching
        `connect()` at once, or one reaching it while the sender is already
        mid-handshake. Both must join the attempt that exists rather than starting
        a second, because two AUTHs racing is both a wasted command against a
        metered API and a way to have one of them answered into a session the
        other has already replaced.
        """
        server.on("AUTH", AUTH_OK)
        link = make_link(server)

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def go():
            barrier.wait()
            try:
                link.connect()
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        # The barrier has exactly as many parties as there are threads, and this
        # one is not among them -- it only waits for them to finish.
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"connect() raised under concurrency: {errors}"
        assert not any(t.is_alive() for t in threads), "a caller was left waiting"
        assert len(server.requests_for("AUTH")) == 1
        assert link.session == "sess1234"

    def test_it_is_distinct_from_reauthenticate(self, server, make_link):
        """`reauthenticate()` drops a live session on purpose; this must not.

        The two read similarly and do opposite things, which is why a startup
        check must not be built on the other one.
        """
        server.on("AUTH", AUTH_OK)

        link = make_link(server)
        link.connect()
        link.reauthenticate()
        _await(lambda: len(server.requests_for("AUTH")) == 2, message="reauthenticate did not log in again")

        assert len(server.requests_for("AUTH")) == 2

    def test_a_refused_credential_is_raised_not_swallowed(self, server, make_link):
        """The whole point: the caller learns at startup rather than later."""
        server.on("AUTH", "500 LOGIN FAILED")

        link = make_link(server)
        with pytest.raises(AniDBAuthFailedError):
            link.connect()

    def test_a_latched_refusal_is_raised_again_without_resending(self, server, make_link):
        """A rejected credential is never offered a second time (SPEC-002)."""
        server.on("AUTH", "500 LOGIN FAILED")

        link = make_link(server)
        with pytest.raises(AniDBAuthFailedError):
            link.connect()
        with pytest.raises(AniDBAuthFailedError):
            link.connect()

        assert len(server.requests_for("AUTH")) == 1

    def test_a_standing_ban_is_raised_and_nothing_is_sent(self, server, make_link):
        """Connecting while backed off must not become the send that deepens it."""
        link = make_link(server)
        link.set_banned(code=555, reason="BANNED")

        with pytest.raises(AniDBBannedError):
            link.connect()

        assert server.received_commands() == []

    def test_the_caller_may_bound_its_own_wait(self, server, make_link):
        """Sixty seconds is longer than an orchestrator will hold a start open.

        The default bound is the transport's handshake budget -- `timeout *
        AUTH_TIMEOUT_FACTOR`, sixty seconds as shipped -- and nothing an embedder
        could reach changed it. A service whose own startup budget is shorter than
        that had no way to say so, and an operator setting one in their own config
        would have been setting a value that did nothing.
        """
        server.on("AUTH", lambda req: None)
        link = make_link(server, timeout=20)

        started = monotonic()
        with pytest.raises(AniDBCommandTimeoutError):
            link.connect(timeout=0.5)
        elapsed = monotonic() - started

        # The point: bounded by what was asked for, not by 20 * 3.
        assert elapsed < 10, f"connect() waited {elapsed:.1f}s despite being given 0.5s"

    def test_giving_up_early_does_not_cancel_the_handshake(self, server, make_link):
        """The property that makes an early bound safe rather than destructive.

        A caller that stops waiting must not leave the transport worse off. The
        handshake carries on, settles, and the next caller joins the result --
        which is why a startup probe can say "tell me within N seconds" without
        having to mean "and abandon the session if not".
        """
        release = threading.Event()

        def slow_auth(req):
            release.wait(5)
            return AUTH_OK

        server.on("AUTH", slow_auth)
        link = make_link(server)

        with pytest.raises(AniDBCommandTimeoutError):
            link.connect(timeout=0.2)

        # Let the handshake AniDB was always going to answer come back.
        release.set()
        _await(lambda: link.session is not None, message="the abandoned handshake never settled")

        # And it was one handshake, not two: the caller left, the attempt did not.
        assert len(server.requests_for("AUTH")) == 1
        link.connect(timeout=5)
        assert len(server.requests_for("AUTH")) == 1

    def test_the_timeout_names_what_actually_happened(self, server, make_link):
        """A wait that ended is not a handshake that failed, and only one is known."""
        server.on("AUTH", lambda req: None)
        link = make_link(server)

        with pytest.raises(AniDBCommandTimeoutError) as raised:
            link.connect(timeout=0.3)

        assert "has not been cancelled" in str(raised.value)

    def test_a_negative_bound_is_refused(self, server, make_link):
        link = make_link(server)

        with pytest.raises(ValueError, match="cannot be negative"):
            link.connect(timeout=-1)

    def test_a_zero_bound_still_answers_when_the_session_is_up(self, server, make_link):
        """`timeout=0` is "tell me now" rather than "wait forever" or "always fail"."""
        server.on("AUTH", AUTH_OK)
        link = make_link(server)
        link.connect()

        link.connect(timeout=0)

        assert len(server.requests_for("AUTH")) == 1

    def test_the_default_is_still_the_transports_own_budget(self, server, make_link):
        """Omitting it must not change what every existing caller gets."""
        server.on("AUTH", AUTH_OK)
        link = make_link(server)

        link.connect()

        assert link.session == "sess1234"

    def test_it_does_not_hang_when_the_handshake_is_never_answered(self, server, make_link):
        """A startup check that blocks forever is worse than no startup check."""
        server.on("AUTH", lambda req: None)

        link = make_link(server, timeout=1)
        started = monotonic()
        with pytest.raises(AniDBError):
            link.connect()

        assert monotonic() - started < 30
