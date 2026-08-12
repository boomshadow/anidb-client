"""Transport tests against a real loopback UDP server.

These cover the paths that only exist because this is a network client: tag
correlation, deflate-compressed replies, session loss and reauthentication, and
the ban handling that must never be provoked against the real service.
"""

import contextlib
import logging
import threading
import time
from concurrent.futures import Future
from time import monotonic

import pytest

import anidb_client
import anidb_client.commands
from anidb_client.errors import AniDBAuthFailedError, AniDBBannedError
from anidb_client.link import AniDBLink
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
            myport=0,
            timeout=kwargs.pop("timeout", 2),
            rate_limiter=RateLimiter(sleep=lambda _seconds: None),
            **kwargs,
        )
        links.append(link)
        return link

    yield factory

    for link in links:
        # Suppressed because a link whose listener already stopped (several tests
        # exercise exactly that) has nothing left to close.
        #
        # AniDBLink's sender thread has no stop short of a LOGOUT round-trip, so it
        # outlives this teardown by design. Tests that deliberately provoke a ban
        # leave it retrying, and it can therefore log one "Failed to send command"
        # on the now-closed socket -- pytest reports that as a thread-exception
        # warning. It is teardown noise, not a leak: the thread is a daemon and the
        # send path handles the error rather than dying on it.
        with contextlib.suppress(Exception):
            link._listener.stop()


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

        # And it still works afterwards. A fresh handshake is only started when
        # something asks for one -- there is no point authenticating into an idle
        # session, least of all against an API that has just asked us to back off.
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

        The attempt is taken and cleared inside the lock, so the loser finds
        nothing to settle rather than finding a settled one.
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
        assert link._auth_attempt is None, "a settled attempt must not stay claimable"

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
