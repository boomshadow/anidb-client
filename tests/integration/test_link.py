"""Transport tests against a real loopback UDP server.

These cover the paths that only exist because this is a network client: tag
correlation, deflate-compressed replies, session loss and reauthentication, and
the ban handling that must never be provoked against the real service.
"""

import contextlib
import logging
import threading
import time
from time import monotonic

import pytest

import anidb_client
import anidb_client.commands
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

        # And it still works afterwards.
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
