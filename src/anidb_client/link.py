#!/usr/bin/env python
#
# This file is part of anidb-client.
#
# anidb-client is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# anidb-client is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with anidb-client.  If not, see <http://www.gnu.org/licenses/>.

import contextlib
import hashlib
import socket
import threading
import zlib
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from time import monotonic
from typing import Any

from Crypto.Cipher import AES

import anidb_client.commands
from anidb_client.commands import Command
from anidb_client.errors import (
    AniDBAuthFailedError,
    AniDBBannedError,
    AniDBCommandTimeoutError,
    AniDBError,
    AniDBInternalError,
    AniDBMustAuthError,
    BanCause,
)
from anidb_client.ratelimit import RateLimiter
from anidb_client.responses import Disposition, Response, ResponseResolver, disposition_for

# The AES cipher objects pycryptodome hands back are one of several mode classes
# with no common base, and this code only ever calls encrypt/decrypt on them. Any
# rather than a union that would have to be widened for every mode never used.
type Cipher = Any

# The outgoing UDP source port this client binds when it is not given one.
#
# Fixed, and deliberately so. AniDB counts requests against a source address, and
# its published guidance is to choose one local port above 1024 at install time
# and reuse it. Above 1024 so no privilege is needed; below the usual Linux
# ephemeral floor of 32768 so the kernel will not hand it to something else on
# the same host. See ADR-007 and SPEC-002.
DEFAULT_OUTGOING_PORT = 9876

# How a refusal reads to a human, per cause. Kept beside the enum's three members
# so that adding a fourth is a mapping that no longer type-checks rather than a
# back-off that describes itself as something it is not.
_REFUSAL_TEXT: dict[BanCause, str] = {
    BanCause.REFUSED: "AniDB asked this client to back off",
    BanCause.SILENCE: "AniDB has stopped answering this client",
    BanCause.LOCAL: "this client could not reach AniDB",
}


class AniDBLink(threading.Thread):
    # How long the sender waits on an empty queue before checking whether the
    # session needs a keepalive.
    IDLE_TICK = 0.2

    # Backstop on waiting for a handshake to settle, as a multiple of the
    # transport timeout. The AUTH command's own timeout normally settles it well
    # inside this; the multiplier exists so that a handshake which somehow
    # settles neither way releases the sender instead of parking it forever.
    AUTH_TIMEOUT_FACTOR = 3

    # Set when an ENCRYPT round trip completes; there is no unencrypted path
    # through _encryption_handler, so it is never assigned otherwise.
    _session_key: bytes

    # How long `stop()` waits for each thread to leave, in seconds.
    #
    # Short on purpose. Both threads are woken rather than merely signalled -- the
    # sender by a notify, the listener by a datagram to itself -- so in the ordinary
    # case they leave in milliseconds and this is never reached. It exists so that a
    # thread wedged somewhere unexpected slows a shutdown down instead of stopping
    # it, which is the containment rule in SPEC-002 applied to shutting down: a
    # library does not get to hold its host process open.
    STOP_JOIN_TIMEOUT = 2.0

    def __init__(
        self,
        user: str,
        pwd: str,
        # host='localhost',
        host: str = "api.anidb.net",
        port: int = 9000,
        myport: int = DEFAULT_OUTGOING_PORT,
        nat_ping_interval: int = 600,
        timeout: int = 20,
        api_key: str | None = None,
        client_name: str | None = None,
        client_version: int | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__()
        self._user = user
        self._pwd = pwd
        # Identity sent in AUTH. Held per-link rather than read from the module
        # globals at send time, so an application embedding this library can
        # authenticate as its own registered client without mutating global state.
        self._client_name = client_name if client_name is not None else anidb_client.anidb_client_name
        self._client_version = client_version if client_version is not None else anidb_client.anidb_client_version
        self._server = (host, port)
        self._queue: deque[Command] = deque()
        # Guards the deque and wakes the sender when something is put on it.
        self._queue_cv = threading.Condition()

        # Outbound pacing and ban back-off. See ratelimit.RateLimiter -- the policy
        # lives there so it can be read and tested without a socket.
        self._rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()

        self._current_tag = 0
        # request() is called from the sender thread, from the listener thread (on a
        # lost session, and on a timeout re-queue) and from whichever application
        # thread asked for data. Handing two of them the same tag would cross one
        # reply onto the other's command.
        self._tag_lock = threading.Lock()
        self._myport = myport
        self._nat_ping_interval = nat_ping_interval
        self._do_ping = False
        # The address AniDB reported back on AUTH nat=1, as (ip, port). It is the
        # only external confirmation a caller can get that the outgoing source
        # port is the one it asked for, so it is kept rather than parsed for the
        # port comparison and thrown away. Read through `reported_address`.
        self._reported_address: tuple[str, int] | None = None
        # When the current session was established, on the monotonic clock. None
        # whenever there is no session. Read through `session_age`.
        self._session_started: float | None = None
        self._listener = AniDBListener(self, myport=myport, timeout=timeout)

        self.timeout = timeout
        self._stop = threading.Event()
        self._authed = threading.Event()
        self._authenticating = threading.Event()
        # Guards the authentication state as a set: the two events above, the
        # session key, and the listener's cipher. Every holder releases it before
        # calling out, so a plain Lock suffices -- but that is a property of the
        # current call graph, and set_banned -> reauthenticate -> _reauthenticate
        # each take it in turn, which is close enough to nesting to be worth saying.
        self._auth_lock = threading.Lock()
        self._session: str | None = None
        # The handshake currently in flight, if any. `_authed` is an Event, and an
        # Event can only ever say "it worked" -- so when AniDB answered AUTH with a
        # refusal there was no signal to give, and the sender waited on an
        # authentication that had already been answered and dropped.
        self._auth_attempt: Future[None] | None = None
        # Set once authentication has failed for a reason retrying cannot change.
        # Latched on purpose: re-sending credentials AniDB has already rejected is
        # one of the surest ways to turn a refusal into a ban.
        self._auth_fatal: AniDBAuthFailedError | None = None
        # Set once the transport has concluded it cannot work at all. Written by
        # the sender as it gives up and read by anyone queueing afterwards, so a
        # request made after the transport died fails immediately instead of
        # joining a queue nothing will ever drain. Only ever goes None -> error.
        self._dead: Exception | None = None

        self._api_key = api_key

        # The listener is started here rather than from its own constructor. It
        # reaches back into this object -- set_banned, reauthenticate, request --
        # and every attribute above this line is one it can touch. Starting it
        # mid-construction meant a reply arriving in that window hit an
        # AttributeError on the listener thread and killed it, after which nothing
        # read the socket and every caller waited on a reply that could not arrive.
        self._listener.start()

        self.daemon = True
        self.start()

    def _logout_handler(self, resp: Response) -> None:
        anidb_client.log.info("Logged out from AniDB")
        self._stop.set()

    def _require_api_key(self) -> str:
        """The configured encryption key.

        Both callers sit on the encrypted path, which _reauthenticate only takes
        when the key is set, so this states the invariant once rather than
        threading a local through the ENCRYPT round trip.
        """
        if self._api_key is None:
            raise AniDBInternalError("Encrypted session requested with no API key configured")
        return self._api_key

    def _start_encrypted_session(self) -> None:
        req = anidb_client.commands.EncryptCommand(self._user, self._require_api_key(), "1")
        self.request(req, self._encryption_handler)

    def _encryption_handler(self, resp: Response) -> None:
        self._session_key = hashlib.md5(bytes(self._require_api_key() + resp.attrs["salt"], "utf-8")).digest()
        self._listener.cipher = AES.new(self._session_key, AES.MODE_ECB)
        anidb_client.log.info("Encrypted session established")
        self._rate_limiter.clear_ban()
        self._send_auth()

    def _send_auth(self) -> None:
        if self._api_key and not self._listener.cipher:
            anidb_client.log.error("Tried to do unencrypted auth but API Key is set!")
            return
        req = anidb_client.commands.AuthCommand(
            self._user, self._pwd, anidb_client.anidb_api_version, self._client_name, self._client_version, nat=1
        )
        self.request(req, self._auth_handler)

    def _reauthenticate(self) -> None:
        with self._auth_lock:
            if self._auth_fatal is not None or self._authenticating.is_set() or self._authed.is_set():
                return
            self._authenticating.set()
            self._auth_attempt = Future()
        try:
            if self._api_key:
                self._start_encrypted_session()
            else:
                self._send_auth()
        except AniDBError as e:
            # The handshake never left the building -- refused by the back-off, or
            # by a socket that is gone. Nothing is going to answer it, so it settles
            # here rather than leaving a waiter on an attempt that was never made.
            with self._auth_lock:
                self._authenticating.clear()
            self._settle_auth(e)

    def _settle_auth(self, error: AniDBError | None) -> None:
        """Release whoever is waiting on the handshake, one way or the other.

        Every path that ends an authentication attempt comes through here, which
        is the whole point: an attempt that ends without settling its future is a
        sender parked on a reply that has already been and gone.

        The settled attempt is left in place rather than cleared. A waiter that
        arrives afterwards has to be able to read *why* it ended -- clearing it
        left that waiter with nothing to look at, so it reported "the handshake did
        not complete" instead of the refusal or the back-off that actually ended
        it, and its caller was retried instead of told. `_reauthenticate` replaces
        it when a genuinely new attempt starts.

        Settled under the lock, so that two threads reaching a conclusion about the
        same attempt -- a reply arriving as the timeout sweep claims it -- cannot
        both get past the done() check. The second settle would raise
        InvalidStateError on whichever lost, which for the listener means the
        socket stops being read.
        """
        with self._auth_lock:
            attempt = self._auth_attempt
            if attempt is None or attempt.done():
                return
            if error is None:
                attempt.set_result(None)
            else:
                attempt.set_exception(error)

    def _auth_handler(self, resp: Response) -> None:
        # Authentication succeeded, so whatever the back-off was for has passed.
        self._rate_limiter.clear_ban()
        # .get, not a subscript. AniDB only returns the address when AUTH asked
        # for it with nat=1, and a reply that parsed without the field raised
        # KeyError here -- on a response thread, where it was invisible, and
        # before anything had been signalled.
        addr = resp.attrs.get("address", "")
        ip, sep, port = addr.rpartition(":")
        reported = (ip, int(port)) if sep and ip and port.isdigit() else None
        if reported is not None and reported[1] != self._myport:
            self._do_ping = True
            anidb_client.log.info(f"NAT detected: will send PING every {self._nat_ping_interval} seconds")
        with self._auth_lock:
            if reported is not None:
                self._reported_address = reported
            self._session_started = monotonic()
            self._authed.set()
            self._authenticating.clear()
        self._settle_auth(None)
        anidb_client.log.info(f"Logged in to AniDB with session {self.session}")

    def auth_failed(self, rescode: str, reason: str, cause: BanCause = BanCause.REFUSED) -> AniDBError:
        """Report that a handshake round trip came back as anything but success.

        Called from the listener thread, which is the only one that sees the
        reply, and from a handshake command's timeout. It settles the waiting
        sender rather than leaving it on an Event that nothing will ever set, and
        hands back the error it settled with so the caller can settle the
        handshake command itself with the same reason.

        Whether another attempt is worth making is decided from the response
        table: a code that means the server is unhappy -- busy, down, banning us
        for now -- backs off and may be retried later. Anything else is a refusal
        of these credentials or this client identity, which no amount of retrying
        will change, so it is latched and no further AUTH is sent.
        """
        error: AniDBError
        if disposition_for(rescode) is not Disposition.NORMAL:
            # register_ban() rather than set_banned(): this runs on the listener
            # thread, and set_banned() re-authenticates, which would send AUTH --
            # and pay the back-off sleep -- from the thread that has to keep
            # reading the socket. The sender re-authenticates on its next command
            # and waits out the back-off there, where waiting is free.
            #
            # Registered before the error is built, because the error carries how
            # long the window it just opened has left to run.
            self._rate_limiter.register_ban(cause)
            # Silence is reported to this method as 604 because that is what the
            # response table classifies -- a handshake that goes unanswered has to
            # come out retryable, not latched, and only a code decides that. But
            # AniDB sent nothing, so the code is a local stand-in for a reply that
            # never came and must not be handed on as one: `rescode` is the code
            # AniDB *answered* with, and a silent ban has none.
            silent = cause is BanCause.SILENCE
            error = AniDBBannedError(
                f"AniDB did not answer the handshake: {reason}"
                if silent
                else f"AniDB refused authentication: {rescode} {reason}",
                cause=cause,
                retry_after=self._rate_limiter.ban_remaining(),
                rescode=None if silent else rescode,
            )
            anidb_client.log.error(f"Backing off: {error}")
        else:
            error = AniDBAuthFailedError(
                f"AniDB refused authentication and retrying will not help: {rescode} {reason}",
                rescode=rescode,
            )
            anidb_client.log.error(str(error))
        with self._auth_lock:
            if isinstance(error, AniDBAuthFailedError):
                self._auth_fatal = error
            self._authed.clear()
            self._authenticating.clear()
            self._session = None
            self._session_started = None
            self._listener.cipher = None
        self._settle_auth(error)
        return error

    def _await_auth(self, timeout: float | None = None) -> None:
        """Block until the handshake settles, raising if it settled as a failure.

        Was `self._authed.wait()` with no timeout and no failure case, which is
        the second half of the reported hang: the first half dropped the reply,
        this half waited for it forever.

        `timeout` bounds **how long this caller waits**, not how long the protocol
        gets. Omitted, the wait is the transport's own handshake budget, which is
        what the sender uses when a command needs a session. Supplied, it is the
        caller's own patience -- shorter changes nothing about the handshake, which
        carries on and settles either way; longer does not extend the protocol's
        budget, because an attempt that fails raises here as soon as it does.
        """
        budget = self.timeout * self.AUTH_TIMEOUT_FACTOR if timeout is None else timeout
        deadline = monotonic() + budget
        while True:
            with self._auth_lock:
                if self._auth_fatal is not None:
                    raise self._auth_fatal
                if self._authed.is_set():
                    return
                attempt = self._auth_attempt
            remaining = deadline - monotonic()
            if remaining <= 0:
                # Named as a wait that ended rather than a handshake that failed,
                # because those are different facts and only one of them is known
                # here. The attempt is still in flight; it will settle, and a later
                # caller joins whatever it settled as.
                raise AniDBCommandTimeoutError(
                    f"Gave up waiting for authentication after {budget:g}s. The handshake has "
                    f"not been cancelled -- it settles on its own, and the next attempt to "
                    f"connect joins the result rather than starting a second one."
                )
            if attempt is None:
                # Nothing in flight and not authenticated: an attempt settled
                # without authenticating us. Waiting longer cannot change that.
                raise AniDBMustAuthError("Authentication did not complete")
            try:
                # Raises whatever failed the attempt, or returns and the loop above
                # confirms the session really is up before any command goes out.
                attempt.result(timeout=remaining)
            except TimeoutError:
                # The attempt has not settled inside what is left of the budget.
                # Loop rather than letting this out: `Future.result` raises
                # `concurrent.futures.TimeoutError`, which is an implementation
                # detail of how the wait is built and says nothing a caller can act
                # on. Going round again reaches the deadline check above, which
                # states the same outcome in this library's own terms.
                #
                # This was unreachable in practice while the only budget was the
                # transport's own: the AUTH command's timeout settled the attempt
                # first, every time. A caller-supplied bound shorter than that
                # reaches it immediately, which is how it surfaced.
                continue

    def _new_tag(self) -> str:
        """Return the next correlation tag, cycling T001..T999.

        UDP gives no ordering guarantee, so the tag is the only thing tying a
        reply back to the command that asked for it.
        """
        with self._tag_lock:
            if self._current_tag >= 999:
                # Was the string "TOOO" -- letters, not zeros -- which was almost
                # certainly meant to be "T000" and had the effect that the rollover
                # tag differed from every other tag's format.
                self._current_tag = 0
                return "T000"
            self._current_tag += 1
            return f"T{self._current_tag:03d}"

    def _ping_callback(self, _resp: Response) -> None:
        anidb_client.log.debug("Successful session refresh")

    def _enqueue(self, command: Command, prio: bool = False) -> None:
        """Put a command on the send queue and wake the sender.

        Priority commands go on the end the sender pops from, so they jump the
        line; ordinary ones go on the far end and are taken in order.
        """
        with self._queue_cv:
            if prio:
                self._queue.append(command)
            else:
                self._queue.appendleft(command)
            self._queue_cv.notify()

    def _take_next_command(self) -> Command | None:
        """The next command to send, or None if the queue stayed empty.

        Was a `while len(queue) < 1: sleep(0.2)` spin. Waiting on a condition
        instead means a queued command is picked up as soon as it is queued rather
        than up to a tick later, and an idle client stops waking 5 times a second
        to look at a deque. The timeout is kept because the idle keepalives below
        are driven by it -- this is a poll for "has enough time passed", which a
        notification cannot express.
        """
        with self._queue_cv:
            if not self._queue:
                self._queue_cv.wait(self.IDLE_TICK)
            return self._queue.pop() if self._queue else None

    def _send_idle_keepalive(self) -> None:
        """Hold the session open while nothing else is going out.

        Called on an empty queue, and outside the queue lock: both branches queue
        a command, which needs that lock.
        """
        if not self._authed.is_set():
            return
        time_since_cmd = self._rate_limiter.seconds_since_last_send()
        # Suppressed rather than handled: a keepalive is housekeeping, and nothing
        # is waiting on one. Sending is refused while a back-off is open, and that
        # refusal must not escape into the loop that keeps the transport running.
        with contextlib.suppress(AniDBError):
            if self._do_ping and time_since_cmd > self._nat_ping_interval:
                self.request(anidb_client.commands.PingCommand(), self._ping_callback)
            elif time_since_cmd >= 1800:
                anidb_client.log.debug("Session idle for 30 minutes, sending UPTIME command")
                self.request(anidb_client.commands.UptimeCommand(), self._ping_callback)

    def run(self) -> None:
        # Checked rather than `while True`: the loop used to have exactly one exit,
        # a LOGOUT reaching the wire, so a transport stopped any other way left this
        # thread waking every idle tick forever. `_teardown` sets the event and
        # notifies the queue, so a stopped sender leaves promptly and by the front
        # door.
        while not self._stop.is_set():
            command = self._take_next_command()
            if command is None:
                self._send_idle_keepalive()
                continue

            anidb_client.log.debug(f"sending command {command.command} with tag {command.tag}")
            if not (self._authed.is_set() or command.command in ("AUTH", "ENCRYPT", "PING")):
                try:
                    # Inside the try because starting a handshake means sending, and
                    # sending is refused while a back-off is open. That refusal is
                    # an answer about this command, not a fault in the loop.
                    self.reauthenticate()
                    self._await_auth()
                except AniDBAuthFailedError as e:
                    # No session is ever coming.
                    anidb_client.log.error(f"Dropping {command.command} ({command.tag}): {e}")
                    self._fail_command(command, e)
                    if command.command == "LOGOUT":
                        break
                    continue
                except AniDBBannedError as e:
                    # A back-off is open, so nothing at all is going out for a
                    # while. Putting the command back would spin this loop at full
                    # speed against a clock that has not moved -- and the answer is
                    # already known. Tell whoever asked; they can come back later,
                    # which is a decision they are better placed to make than a
                    # queue that would hold them for half an hour to make it.
                    anidb_client.log.warning(f"Refusing {command.command} ({command.tag}): {e}")
                    self._fail_command(command, e)
                    if command.command == "LOGOUT":
                        break
                    continue
                except AniDBError as e:
                    # The handshake may still work later -- the API did not answer,
                    # or answered something unusable. Put the command back rather
                    # than losing it; it stays registered under the same tag. There
                    # is nothing to log out of if we never got in.
                    if command.command == "LOGOUT":
                        break
                    anidb_client.log.warning(f"Requeueing {command.command} ({command.tag}): {e}")
                    self._enqueue(command, prio=True)
                    continue

            try:
                self._send_command(command)
            except AniDBInternalError as e:
                # The transport itself is gone -- the listener has stopped, so
                # nothing will read a reply to anything. This used to escape and
                # end the sender thread, which released nobody: every command
                # already queued had no send time, the timeout sweep skips those,
                # and every caller waited on a reply that could not be read even
                # if it arrived. The check said "kill the main thread if the
                # listener dies" and killed the one thread that could have.
                anidb_client.log.error(f"Transport has failed; abandoning every command in flight: {e}")
                self._dead = e
                self._fail_command(command, e)
                self._abort_pending(e)
                break
            except AniDBError as e:
                anidb_client.log.error(f"Cannot send {command.command} ({command.tag}): {e}")
                self._fail_command(command, e)
                continue

            if command.command == "LOGOUT":
                break

    def _fail_command(self, command: Command, error: Exception) -> None:
        """Drop a command and tell whoever asked for it.

        Unregistering matters as much as failing: a command that never went out
        has no send time, and the timeout sweep skips those, so one left in the
        table would sit there unanswered and unswept for the life of the process.
        """
        self._listener.pop_command(command.tag)
        command.fail(error)

    def _abort_pending(self, error: Exception) -> None:
        """Fail everything outstanding, queued or awaiting a reply.

        The containment rule in its strongest form: when the transport concludes
        it can no longer work, that conclusion has to reach every caller it was
        working for. Silence is the one outcome a caller cannot act on.
        """
        with self._queue_cv:
            queued = list(self._queue)
            self._queue.clear()
        for command in queued:
            self._fail_command(command, error)
        for tag, command in self._listener.pending_commands():
            if self._listener.pop_command(tag) is not None:
                command.fail(error)

    def _send_command(self, command: Command) -> None:
        # Checked before pacing, and before anything touches the socket. While the
        # back-off is open nothing goes out at all -- not this command, not the
        # authentication it would trigger. The transport used to *sleep* here
        # instead, holding the command and its thread for the length of the ban;
        # when the thread was the listener, that stopped the socket being read.
        #
        # Refusing rather than waiting is also the gentler of the two. The waiting
        # version still intended to send, and did, the moment the clock allowed it.
        # This one sends nothing and says so, and whoever asked can decide when to
        # come back.
        refusal = self.refusal()
        if refusal is not None:
            raise refusal
        self._rate_limiter.wait()
        # `sock is None` as well as thread liveness: stop() closes the socket
        # before the listener thread has finished winding down, and a command
        # timing out in that window would otherwise call sendto() on None.
        if not self._listener.is_alive() or self._listener.sock is None:
            anidb_client.log.error("Listener has died; aborting")
            raise AniDBInternalError("Listener has died")
        # Read once and reused below. Testing `self._session` here and reading it
        # again at authorize() let the listener thread clear the session in between,
        # so a command could pass this check and then be authorized with None.
        session = self.session
        if not session and command.command not in ("AUTH", "PING", "ENCRYPT"):
            raise AniDBMustAuthError(f"You must be authed to execute command {command.command}")
        if command.command == "AUTH" and self._authed.is_set():
            anidb_client.log.warning("Attempted double auth; ignoring")
            return
        elif command.command == "ENCRYPT" and self._listener.cipher:
            anidb_client.log.warning("Attempted double encrypt command; ignoring")
            return
        command.authorize(session)
        self._rate_limiter.record_send()
        command.started = monotonic()
        # Counted here rather than in request(), which is also the re-queue path:
        # what the budget bounds is how many times this command reaches AniDB, not
        # how many times it went round the queue.
        command.attempts += 1
        data = command.raw_data().encode("utf-8")
        # One read, handed to encrypt() so the test and the use cannot disagree.
        cipher = self._listener.cipher
        if cipher:
            data = self._listener.encrypt(data, cipher)

        if command.command == "AUTH":
            anidb_client.log.debug("NetIO > AUTH data is not logged!")
        else:
            anidb_client.log.debug(f"NetIO > {repr(data)}")

        try:
            self._listener.sock.sendto(data, self._server)
        except OSError as e:
            # Was `socket.gaierror` alone, which is one subclass of OSError and
            # covers only name resolution. Anything else -- most often the socket
            # being closed by stop() between the liveness check above and this call
            # -- escaped and killed the sender thread silently. Every case wants
            # the same treatment: log it, put the command back, and back off.
            anidb_client.log.warning(f"Failed to send command {command.command}: {e}")
            if command.command not in ("AUTH", "PING", "ENCRYPT"):
                self._enqueue(command, prio=True)
            # LOCAL: nothing was asked of AniDB. The back-off is this client
            # protecting itself from spinning on a socket that is not working,
            # not AniDB refusing anything.
            self.set_banned(reason=b"Network unavailable", cause=BanCause.LOCAL)

    def request(self, command: Command, callback: Callable[[Response], None], prio: bool = False) -> Future[Response]:
        """Queue a command and hand back its outcome.

        The returned future settles when the reply has been handled, or fails when
        the transport concludes no reply is coming. Callers that only want the
        side effect the callback performs may ignore it; callers that need to know
        whether it happened cannot get that from a callback, because the case that
        matters is the one where no callback ever runs.

        Re-queueing an existing command reuses its future -- the caller is waiting
        on the request, not on any one attempt at it.
        """
        command.started = None
        command.callback = callback
        command.tag = self._new_tag()
        if self._dead is not None:
            # Nothing drains the queue any more. Say so now rather than accepting
            # the command and letting its caller wait out a timeout for an answer
            # that was never possible.
            command.fail(self._dead)
            return command.future
        self._listener.queue_command(command)
        anidb_client.log.debug(f"Queued command {command.command} with tag {command.tag}")
        if command.command in ("ENCRYPT", "AUTH", "PING"):
            self._send_command(command)
            return command.future
        self._enqueue(command, prio=prio)
        return command.future

    @property
    def session(self) -> str | None:
        """The current session key, or None.

        Read through the lock because it is written by the listener thread (on a
        successful AUTH, and cleared on a lost session) and read by the sender
        thread on every command. Callers that both test it and use it must read it
        once through here rather than touching `_session` twice.
        """
        with self._auth_lock:
            return self._session

    def set_session(self, session: str | None) -> None:
        with self._auth_lock:
            self._session = session
            self._session_started = monotonic() if session else None

    # ---- health surface -------------------------------------------------
    #
    # Read-only, and answered from state the transport already keeps. An
    # application embedding this library has to be able to tell "quiet because
    # there is nothing to do" from "quiet because AniDB has closed the gate", and
    # the only ways to find out used to be to reach into private attributes or to
    # send a command -- which, during a ban, is the one thing not to do.

    @property
    def is_banned(self) -> bool:
        """True while a back-off has been registered and not yet cleared.

        Stays true after the window has elapsed: the multiplier is only cleared
        by an authentication that succeeds, so this reports "we are in trouble"
        rather than "we may not send right now". For the second, read
        `ban_remaining`.
        """
        return self._rate_limiter.is_banned

    @property
    def ban_cause(self) -> BanCause | None:
        """Which of the three refusals opened the current back-off, or None."""
        return self._rate_limiter.ban_cause

    @property
    def ban_remaining(self) -> float:
        """Seconds until anything may be sent again, unrounded. 0 if it may now."""
        return self._rate_limiter.ban_remaining()

    @property
    def ban_multiplier(self) -> int:
        """How many times the back-off has doubled. 0 when there is no ban."""
        return self._rate_limiter.ban_multiplier

    @property
    def session_age(self) -> float | None:
        """Seconds since the current session was established, or None if there is none."""
        with self._auth_lock:
            started = self._session_started
        return None if started is None else monotonic() - started

    @property
    def reported_address(self) -> tuple[str, int] | None:
        """The (ip, port) AniDB last reported seeing, or None if it never did.

        AniDB returns this only when AUTH asked for it with nat=1, and only when
        the reply carries something that reads as an address. It is advisory --
        a login that succeeded on the wire is not undone by its absence -- but it
        is the only outside confirmation that the outgoing source port is the one
        this client asked for.
        """
        with self._auth_lock:
            return self._reported_address

    def refusal(self) -> AniDBBannedError | None:
        """The open back-off as an error, or None if sending is allowed.

        One place builds it, so everything told to back off is told the same
        things: how long is left, unrounded, and which refusal it is. The
        transport used to format that into a message here and nowhere else, and
        rounded to whole minutes -- which read as "0 minutes remaining" for any
        window shorter than half of one.
        """
        remaining = self._rate_limiter.ban_remaining()
        if remaining <= 0:
            return None
        cause = self._rate_limiter.ban_cause or BanCause.REFUSED
        return AniDBBannedError(
            f"{_REFUSAL_TEXT[cause]}; nothing will be sent for another {remaining:.0f}s",
            cause=cause,
            retry_after=remaining,
        )

    def connect(self, timeout: float | None = None) -> None:
        """Establish the session now, or raise the reason it cannot be.

        The transport authenticates lazily: nothing reaches AniDB until a command
        needs a session, so a wrong password or a standing ban is discovered by
        whichever request happens to be first. For a script that is fine. For a
        long-running service it is the difference between refusing to start and
        starting, looking healthy, and failing in front of a user an hour later.
        This is how such an application asks the question at boot instead.

        **Idempotent.** A session already up is left alone, and a handshake already
        in flight is waited on rather than duplicated -- neither starts a second
        one. That is deliberate rather than incidental: against an API that meters
        by request frequency, a call that logs in again every time it is invoked is
        a way to get banned, and the shape most likely to invite that is exactly
        this one. `reauthenticate()` is the other method, and it is not this: it
        drops a live session on purpose, which is what a *lost* session needs and
        what a startup check must not do.

        **This adds no traffic in the ordinary case.** The handshake happens either
        way; calling this moves it from the first request to startup. Only a
        process that connects and then never asks for anything pays for a login it
        did not otherwise need.

        **It is not a health check, and must not be polled.** Every command here is
        metered by a service whose enforcement is an IP ban, so a readiness probe
        calling this on a timer is a machine for earning one. Read the health
        surface instead -- `is_banned`, `ban_cause`, `ban_remaining`,
        `session_age` -- which answers out of state already held and sends nothing.

        Raises whatever stopped it: the refusal AniDB gave, the back-off that
        forbade sending, or a timeout.

        **`timeout` bounds how long the caller waits, not how long the protocol
        gets**, and those are genuinely different. Omitted, the wait is the
        transport's own handshake budget -- `self.timeout * AUTH_TIMEOUT_FACTOR`,
        sixty seconds by default, which is a long time to hold a container's
        startup. Supplied, it is this caller's patience, and giving up early is
        safe by construction: the handshake is not cancelled, it settles on its
        own, and the next call joins whatever it settled as rather than starting a
        second one. That is exactly what a startup probe wants -- *tell me within N
        seconds whether this is up, and do not break anything if I stop asking.*

        Deliberately not `self.timeout`. That value is the per-command reply
        timeout: it also drives the listener's socket, the timeout sweep and
        therefore the retry budget, so lowering it to bound a startup check would
        change how every command behaves as a side effect. Wanting a shorter
        startup is not wanting fewer retries.
        """
        # `_reauthenticate` rather than `reauthenticate`: the private one already
        # declines to act when a session is up, when one is being established, or
        # when credentials have been latched as refused. That is precisely the
        # idempotence this method promises, so it is reused rather than restated.
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout is how many seconds to wait and cannot be negative: {timeout!r}")
        self._reauthenticate()
        self._await_auth(timeout)

    def reauthenticate(self) -> None:
        # One critical section: a half-cleared state -- session gone but cipher
        # still set, or the reverse -- is a command encrypted with a key the server
        # has forgotten, or an unencrypted command on a session that requires one.
        with self._auth_lock:
            self._authed.clear()
            self._session = None
            self._session_started = None
            self._listener.cipher = None
        self._reauthenticate()

    def stop(self, timeout: float | None = None, logout: bool = True) -> None:
        """End the transport and release what it holds.

        **Logging out is best effort; ending is not.** This used to be a choice
        between the two, and neither branch of it finished the job. Authenticated,
        it sent LOGOUT and waited -- and then returned without ever closing the
        socket, so a clean `init()`/`close()` cycle left the pinned source port
        bound for the life of the process and the listener thread reading it.
        Unauthenticated, it closed the socket but never signalled the sender, which
        went on waking every idle tick forever. There was no path through here that
        stopped both threads and gave the port back, which is what `close()` is
        documented to do (SPEC-006).

        So the courtesy is attempted under `logout`, and the teardown runs in a
        `finally` regardless of how the courtesy went. That also bounds the case
        that hurts an embedder most: a client AniDB has stopped answering can never
        be told about the logout, so the wait ran to the full command timeout and
        *then* left everything running. Now the wait is bounded by `timeout`,
        which the caller may shorten, and the teardown happens either way.

        `logout=False` is the half-built client: a failed `init()` tearing down
        something that never authenticated. A politeness packet from a client with
        no session is worse than silence, and while a back-off is open it is the
        one thing that must not go out at all.
        """
        if timeout is None:
            timeout = self.timeout
        try:
            if logout and self._authed.is_set():
                anidb_client.log.debug("Logging out from AniDB")
                # Suppressed: the transport may be banned, dead or mid-handshake,
                # and every one of those is a reason the courtesy cannot be paid --
                # not a reason to leave the socket bound.
                with contextlib.suppress(AniDBError):
                    req = anidb_client.commands.LogoutCommand()
                    self.request(req, self._logout_handler)
                    self._stop.wait(timeout)
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Stop both threads, close the socket, and fail everything still waiting.

        Sends nothing, and is safe to run twice -- `close()` may be called on a
        client that has already been closed, and a failed `init()` tears down
        through here as well.

        The source port is available again before this returns; that is the
        property a caller retrying `init()` depends on, now that the port is pinned
        and not shareable (ADR-007). Closing the socket is not by itself enough to
        deliver it -- see `AniDBListener.stop`, which is where the waking and the
        closing happen in the order that makes it true.

        Each thread is signalled, woken, and then joined with a bounded timeout.
        Waking is what makes the join short: a thread parked in `recv` or on a
        condition would otherwise be joined for as long as its own timeout, which
        is the wait a shutdown must not take. The bound is what keeps the join from
        becoming that wait anyway if a thread is wedged somewhere unexpected --
        both are daemons and neither holds anything by then, so a shutdown is
        slowed rather than prevented.

        Failing what is still in flight is the containment rule in SPEC-002 -- a
        waiter is always released. A command outstanding when the transport stops
        can never be answered now, and leaving it registered would hang whoever
        asked for it rather than telling them.
        """
        self._stop.set()
        # Wake the sender out of its idle wait rather than letting it find out on
        # the next tick, so nothing is still moving while the socket is closed.
        with self._queue_cv:
            self._queue_cv.notify_all()
        self._listener.stop()
        self._abort_pending(AniDBInternalError("The transport has been stopped"))
        # The sender holds no descriptor, so this is about the guarantee rather than
        # about the port: it is waiting on a condition that has just been notified,
        # and joining it is what makes "nothing is running" true at the moment this
        # returns rather than shortly afterwards. Bounded, because a sender part-way
        # through a paced send is not worth stalling a shutdown for -- it is a daemon
        # and it holds nothing.
        # `is_alive` as well as the identity check: a thread that was never started
        # cannot be joined at all, and the transport is torn down from paths where
        # that is the case -- a construction that failed part-way, and a `close()`
        # on a client that has already been closed.
        if self.is_alive() and threading.current_thread() is not self:
            self.join(self.STOP_JOIN_TIMEOUT)

    def set_banned(
        self,
        code: int | None = None,
        reason: bytes | str | None = None,
        cause: BanCause = BanCause.REFUSED,
    ) -> AniDBBannedError:
        """Open a back-off window, and hand back the refusal it opened.

        Returning it is what lets a caller settle the command that provoked the
        ban with the same reason the next caller will be given, rather than
        inventing a second description of one situation. Nothing here sleeps, so
        registering before failing costs the waiting caller nothing -- and the
        error cannot state how long is left until the window it describes exists.

        `code` is the AniDB response code when there was one. Silence has none,
        and neither does a datagram that never left this host.
        """
        # Decoded rather than interpolated: the reasons raised from commands.py are
        # bytes literals, which formatted as b'API not responding' in the log line.
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        self._rate_limiter.register_ban(cause)
        detail = f"{code} {reason}" if code is not None else str(reason)
        anidb_client.log.error(
            f"Backing off ({cause.name.lower()}): {detail} "
            f"(nothing will be sent for {self._rate_limiter.ban_remaining():.0f}s)"
        )
        # The session is dropped but no new one is started here. This runs on the
        # listener thread for an untagged ban notice and for a command that timed
        # out, and starting a handshake means sending -- which, until the back-off
        # is over, is the one thing that must not happen, and which used to happen
        # by sleeping out the back-off on the thread that had to keep reading the
        # socket. The next command that needs a session drives the next handshake,
        # once the transport is allowed to send again.
        with self._auth_lock:
            self._authed.clear()
            self._authenticating.clear()
            self._session = None
            self._session_started = None
            self._listener.cipher = None
        return AniDBBannedError(
            f"{_REFUSAL_TEXT[cause]}: {detail}",
            cause=cause,
            retry_after=self._rate_limiter.ban_remaining(),
            rescode=str(code) if code is not None else None,
        )


class AniDBListener(threading.Thread):
    # How many commands must time out in a row, with nothing arriving from AniDB
    # in between, before the transport treats the silence as a ban.
    #
    # Two, because one is ordinary. UDP loses datagrams, and a single dropped
    # reply is exactly what the retry budget exists to absorb. Two in a row with
    # no traffic at all in between is not loss -- it is the API declining to
    # answer, which is AniDB's documented enforcement: it drops packets from a
    # banned client rather than replying to say so. A client that waits for a
    # ban notice that is never sent keeps sending into the ban.
    SILENT_TIMEOUTS_BEFORE_BAN = 2

    def __init__(self, sender: AniDBLink, myport: int = DEFAULT_OUTGOING_PORT, timeout: int = 20) -> None:
        super().__init__()

        self.timeout = timeout
        self.sock: socket.socket | None = self._connect_socket(myport, self.timeout)
        self._sender = sender
        # Written by whichever thread completes an ENCRYPT or drops the session,
        # read by this thread on every packet. Behind an accessor rather than being
        # reached into from AniDBLink, which is what it was.
        self._cipher_lock = threading.Lock()
        self._cipher: Cipher | None = None
        # When a reply this client could read last arrived. Drives the re-queue
        # branch of the timeout sweep: a command sent before this timed out while
        # the API was demonstrably answering, so something else is going on --
        # most likely a re-authentication -- and it goes back on the queue rather
        # than being counted against its budget.
        self._last_receive = monotonic()
        # When a datagram last arrived at all -- tagged, untagged, or unreadable.
        # Deliberately not the same thing as `_last_receive`: what the silence
        # detector measures is whether anything is coming back from AniDB, and a
        # packet this client could not parse is still a server that is answering.
        # Both are touched only by this thread, which runs the receive loop and
        # the timeout sweep alike, so neither needs a lock.
        self._last_datagram = self._last_receive
        # Commands that have timed out since the last datagram arrived. The
        # silence detector's whole state.
        self._silent_timeouts = 0
        self._stopping = threading.Event()

        self.cmd_queue: dict[str, Command] = {}
        # The sender thread inserts into cmd_queue while this thread reads, pops and
        # iterates it. Iterating a dict that another thread is inserting into raises
        # RuntimeError, and a RuntimeError raised here ends the listener -- after
        # which nothing reads the socket, no callback ever runs, and every caller
        # waits on an event that can no longer be set. Both threads go through the
        # three accessors below rather than touching the dict directly.
        self._queue_lock = threading.Lock()

        # Not started here: AniDBLink starts it once its own construction is
        # finished. See the comment at that call.
        self.daemon = True

    @property
    def cipher(self) -> Cipher | None:
        with self._cipher_lock:
            return self._cipher

    @cipher.setter
    def cipher(self, value: Cipher | None) -> None:
        with self._cipher_lock:
            self._cipher = value

    def queue_command(self, command: Command) -> None:
        """Register a command so its reply can be matched back to it."""
        with self._queue_lock:
            self.cmd_queue[command.tag] = command

    def pop_command(self, tag: str) -> Command | None:
        """Claim the command awaiting `tag`, or None if nothing is waiting.

        Atomic on purpose: the callers used to test membership and then pop as two
        steps, which the timeout sweep running on this same thread could interleave.
        """
        with self._queue_lock:
            return self.cmd_queue.pop(tag, None)

    def pending_commands(self) -> list[tuple[str, Command]]:
        """A snapshot of the outstanding (tag, command) pairs, safe to iterate."""
        with self._queue_lock:
            return list(self.cmd_queue.items())

    def _connect_socket(self, myport: int, timeout: int) -> socket.socket:
        """Bind the one socket this client sends and receives on.

        **Without SO_REUSEADDR.** On Linux that option lets a second process bind
        a port this one already holds, with no error on either side; the kernel
        then delivers each datagram to one of them. The starved client sees its
        replies go missing, retries into the silence, and earns a ban for a
        problem it has no way to see. A duplicate bind is a mistake, and it is
        cheaper to find out at startup than from AniDB, so it is left to fail.

        The failure is turned into something that names the cause: a bare
        EADDRINUSE from a library's constructor says nothing about which port, or
        why this client insists on one.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.bind(("", myport))
        except OSError as e:
            sock.close()
            raise AniDBError(
                f"Cannot bind outgoing UDP port {myport}: {e}. This client pins one source "
                f"port and sends from it alone, because AniDB counts requests against a "
                f"source address; sharing the port with another process would silently "
                f"split the replies between them. Give this client a port of its own, or "
                f"stop whatever already holds this one."
            ) from e
        return sock

    def _disconnect_socket(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def encrypt(self, data: bytes, cipher: Cipher) -> bytes:
        # The cipher is passed in rather than read here, so the caller's `if cipher`
        # test and this use are the same read. Reading it again would let the
        # session drop in between -- and unlike the receive path, which can suppress
        # the failure and treat the packet as plaintext, there is nothing sensible to
        # do about it here except end the sender thread. So the race is removed
        # rather than handled.
        pad_len = 16 - len(data) % 16
        padding = (chr(pad_len) * pad_len).encode("utf-8")
        data = data + padding
        encrypted: bytes = cipher.encrypt(data)
        return encrypted

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt one packet, raising ValueError if it is not an encrypted reply.

        ValueError specifically, because run() suppresses exactly that and falls
        back to reading the packet as plaintext -- which AniDB really does send on
        an encrypted session, an untagged ban notice being the case that matters.
        Any other exception type would escape that suppression and end the listener
        thread, and a listener that has stopped reading the socket is a permanent
        hang for every caller waiting on a reply.
        """
        # ValueError rather than an AttributeError on None: run() suppresses exactly
        # ValueError and falls back to reading the packet as plaintext, which is what
        # a packet arriving after the session dropped actually is. An AttributeError
        # would escape and end the listener thread.
        cipher = self.cipher
        if cipher is None:
            raise ValueError("No cipher established; packet cannot be an encrypted reply")
        if not data:
            raise ValueError("Empty packet cannot be decrypted")
        data = cipher.decrypt(data)
        # PKCS#5: the final byte gives the padding length, and the padding is that
        # byte repeated. Neither holds for a plaintext packet whose length happens
        # to be a multiple of the block size -- which was previously truncated by
        # whatever its last byte said and the remains parsed as though a reply.
        pad_len = data[-1]
        if not 1 <= pad_len <= 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
            raise ValueError("Packet does not carry valid PKCS#5 padding; not an encrypted reply")
        return data[:-pad_len]

    def stop(self) -> None:
        """Leave the receive loop and give the port back.

        **Closing the socket is not enough, and this is the whole reason this
        method is more than two lines.** A thread blocked in `recv` keeps the
        underlying socket alive in the kernel even after the descriptor is closed:
        the bound port is not released until that call returns. Measured, not
        assumed -- bind a UDP port, block a thread in `recv` on it, close it from
        another thread, and the rebind fails with EADDRINUSE.

        That is survivable when the port is ephemeral and shareable. It is not
        survivable here. This client pins one source port and no longer sets
        SO_REUSEADDR (ADR-007), so a listener still sitting in `recv` holds the only
        port the next client may use -- for up to the socket timeout, which is
        measured in seconds and looks exactly like the leak that has nothing to do
        with it. `close()` promises the port back (SPEC-006); this is what makes
        that true rather than eventually true.

        So the loop is signalled, then *woken*, then waited for, and only then is
        the socket closed. The wake is a zero-length datagram this socket sends to
        itself over loopback -- the standard way to interrupt a blocking `recv`,
        and worth being unambiguous about: **it is addressed to this process, never
        to AniDB.** Nothing on the teardown path may reach the service, least of all
        a client that never authenticated or one that is backing off.
        """
        anidb_client.log.debug("Closing listening socket")
        # Signalled before anything else, so the loop below can tell a deliberate
        # shutdown from a transient socket error -- and so the woken loop finds the
        # flag already set rather than racing it.
        self._stopping.set()
        self._wake()
        # See the note on the sender's join: a listener that was never started --
        # which is how it is constructed, deliberately, so a reply cannot arrive
        # mid-construction -- raises rather than returning from join().
        if self.is_alive() and threading.current_thread() is not self:
            self.join(AniDBLink.STOP_JOIN_TIMEOUT)
        self._disconnect_socket()

    def _wake(self) -> None:
        """Nudge the receive loop out of `recv` by sending this socket a datagram.

        Sent from a throwaway socket rather than from the one being torn down, so
        this never touches a descriptor the listener thread is using. Addressed to
        loopback explicitly: a socket bound to all interfaces reports its own
        address as 0.0.0.0, which is not a destination.

        Best effort. If the socket is already gone, or the datagram cannot be sent,
        the loop still leaves on its own timeout -- slower, but not wrong.
        """
        sock = self.sock
        if sock is None:
            return
        with contextlib.suppress(OSError):
            port = sock.getsockname()[1]
            waker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                waker.sendto(b"", ("127.0.0.1", port))
            finally:
                waker.close()

    def run(self) -> None:
        while not self._stopping.is_set() and self.sock:
            try:
                # settimeout is inside the try: stop() closes this socket from
                # another thread, and calling settimeout on the closed descriptor
                # raised an unhandled OSError out of this thread.
                self.sock.settimeout(self.timeout)
                anidb_client.log.debug(f"Listening on socket with {self.sock.gettimeout()}s timeout")
                data = self.sock.recv(8192)
            except TimeoutError:
                self._handle_timeouts()
                continue
            except OSError, AttributeError:
                # AttributeError covers stop() setting self.sock to None between
                # the loop check and the call above.
                if self._stopping.is_set() or self.sock is None:
                    return
                continue
            # Checked here as well as at the top of the loop, because the datagram
            # just received may be the one stop() sent to wake this thread. Falling
            # through would parse it -- harmlessly, the loop survives garbage -- and
            # then take another turn, which is a turn the shutdown is waiting on.
            if self._stopping.is_set():
                return
            self._note_datagram()
            anidb_client.log.debug(f"NetIO < {repr(data)}")
            if self.cipher:
                with contextlib.suppress(ValueError):
                    data = self.decrypt(data)
            # A reply prefixed with two zero bytes is deflated (the AUTH comp=1
            # option). This ran twice in a `for i in range(2)` loop, doing
            # identical work both times and discarding the first result; the
            # guard that followed it could never be true, because ResponseResolver
            # either returns an object or raises.
            payload = data
            if payload[:2] == b"\x00\x00":
                payload = zlib.decompressobj().decompress(payload[2:])
                anidb_client.log.debug(f"UnZip | {repr(payload)}")
            try:
                resolved = ResponseResolver(payload)
            except (UnicodeDecodeError, ValueError) as e:
                anidb_client.log.warning(f"Unparsable response from API ({e}): {repr(data)}")
                continue

            # Disposition first, tag second. A code that says stop is a statement
            # about the connection, not about whichever command it happens to
            # carry the tag of, so it must close the gate whether it arrives
            # untagged, correctly tagged, or wrongly tagged. AniDB's own
            # documentation notes that 555 "sometimes uses the wrong tag", and a
            # wrongly-tagged 555 used to be handed to a caller as an ordinary
            # successful reply: no ban registered, no back-off opened, and the
            # sender still firing into a service that had just said stop.
            #
            # The verdict comes from the response table in responses.py, which is
            # where AniDB's contract is transcribed. The transport keeps no list
            # of its own; it used to, and the restatement disagreed with the table.
            cmd = self.pop_command(resolved.restag) if resolved.restag else None
            disposition = disposition_for(resolved.rescode)
            if disposition is not Disposition.NORMAL:
                self._handle_refusal(resolved, disposition, cmd)
                continue
            if not resolved.restag:
                self._last_receive = monotonic()
                self._handle_untagged(resolved, data)
                continue
            if cmd is None:
                continue
            resp = resolved.resolve(cmd)
            resp.parse()
            self._last_receive = monotonic()
            if cmd.command in ("AUTH", "ENCRYPT") and not self._is_successful_handshake(cmd, resp):
                continue
            if resp.rescode in ("200", "201"):
                # Safe to subscript: the check above returned True only for a
                # handshake reply that carries this field.
                self._sender.set_session(resp.attrs["sesskey"])
            elif resp.rescode in ("501", "506", "403"):
                if cmd.command == "LOGOUT":
                    self.stop()
                else:
                    anidb_client.log.warning("Lost session with AniDB; attempting to reauthenticate")
                    try:
                        self._sender.reauthenticate()
                        self._sender.request(cmd, cmd.callback, prio=True)
                    except AniDBError as e:
                        # Re-authenticating means sending, which is refused while a
                        # back-off is open. On this thread that refusal would end
                        # the listener, and a listener that has stopped reading the
                        # socket is a permanent hang for every caller.
                        anidb_client.log.error(f"Cannot recover the session right now: {e}")
                        cmd.fail(e)
                continue
            elif resp.rescode in ("203", "500", "503"):
                self.stop()

            resp_thread = threading.Thread(target=self._deliver, args=(cmd, resp))
            resp_thread.daemon = True
            resp_thread.start()

    def _note_datagram(self) -> None:
        """Record that AniDB is still talking to us.

        Any datagram counts -- a reply to something else, an untagged notice, or
        a packet that turns out to be readable by nobody. All three say the same
        thing about the connection: datagrams from this client are still reaching
        a server that still sends some back, so whatever else is wrong, this is
        not the silent drop AniDB enforces a ban with.

        Recorded here, once, immediately after the socket read, rather than on
        each of the several paths a packet takes through the loop below. A
        detector that had to be told about every one of them would eventually
        miss one, and the path it missed would be a live API looking silent.
        """
        self._last_datagram = monotonic()
        self._silent_timeouts = 0

    def _handle_refusal(self, resolved: ResponseResolver, disposition: Disposition, cmd: Command | None) -> None:
        """Close the gate for a reply that says stop, and settle what it answered.

        Both halves are required. The gate is what keeps the sender quiet; the
        settlement is SPEC-002's rule that every request carries an outcome --
        the reply, or the reason there will not be one. A refusal that closed the
        gate and left its command in flight would trade one hang for another.
        """
        code = resolved.rescode
        reason = resolved.resstr
        self._last_receive = monotonic()
        anidb_client.log.warning(f"API says {code} {reason} ({disposition.name})")
        if cmd is not None and cmd.command in ("AUTH", "ENCRYPT"):
            # The handshake has its own path, which registers the ban *and*
            # settles the attempt the sender is parked on. Doing it here as well
            # would count one refusal twice and double the back-off for it.
            cmd.fail(self._sender.auth_failed(code, reason))
            return
        refusal = self._sender.set_banned(code=int(code), reason=reason, cause=BanCause.REFUSED)
        if cmd is not None:
            cmd.fail(refusal)

    def _handle_untagged(self, resolved: ResponseResolver, data: bytes) -> None:
        """An ordinary code that answers nothing: the server volunteering something.

        A refusal never reaches here -- those are classified before the tag is
        looked at. What is left is the encrypted session expiring, and codes this
        table has never seen, which are logged and moved past. They are not
        guessed at, and in particular they are not assumed to be a ban.
        """
        if resolved.rescode == "598":
            # We get here if an encrypted session has timed out
            # No need to log in again if all that's left in queue is a
            # logout command.
            if all(x.command == "LOGOUT" for _tag, x in self.pending_commands()):
                self.stop()
                return
            anidb_client.log.warning("Lost encrypted session with AniDB; attempting to reauthenticate")
            # Suppressed for the reason given on the tagged session-loss path:
            # re-authenticating sends, sending is refused during a back-off, and
            # that refusal reaching this thread would end the listener.
            with contextlib.suppress(AniDBError):
                self._sender.reauthenticate()
            return
        # Previously sys.exit(2). An untagged reply we do not recognise is worth
        # shouting about, but it is not worth killing the caller's process over.
        anidb_client.log.error(f"Unhandled response from API: {repr(data)}")

    def _deliver(self, cmd: Command, resp: Response) -> None:
        """Run a reply's callback, then settle the command it answers.

        In that order, so a caller released by the outcome finds the callback's
        work -- the cache write, in practice -- already done.

        A callback that raises used to end here as an unhandled exception in a
        thread nobody joins: logged by the interpreter at shutdown, invisible at
        the time, and leaving whoever asked for the command waiting on a reply
        that had in fact arrived and been mishandled. It is the same hang as a
        reply that never came, from the other direction.
        """
        try:
            resp.handle()
        except Exception as e:
            anidb_client.log.exception(f"Handler for {cmd.command} ({cmd.tag}) failed")
            cmd.fail(e)
            return
        cmd.succeed(resp)

    def _is_successful_handshake(self, cmd: Command, resp: Response) -> bool:
        """True if this AUTH or ENCRYPT reply succeeded and may reach its handler.

        A whitelist of the codes that mean success, rather than a list of the ones
        that mean failure. The failure list is the thing that cannot be kept
        complete -- and when it was incomplete, a refusal fell through to the
        success handler, whose first act was to read a field only a successful
        reply carries. That raised on this thread, nothing was signalled, and the
        sender waited forever on a handshake that had already been answered.

        The required field is checked too, not just the code: a success code
        without its session key or salt is not something the handlers below can
        use, and finding that out by raising inside them is exactly the failure
        this exists to prevent.
        """
        if cmd.command == "AUTH":
            if resp.rescode in ("200", "201") and resp.attrs.get("sesskey"):
                return True
        elif resp.rescode == "209" and resp.attrs.get("salt"):
            return True
        self._sender.auth_failed(resp.rescode, resp.resstr)
        return False

    def _handle_timeouts(self) -> None:
        willpop = []
        cmd = None
        now = monotonic()
        for tag, cmd in self.pending_commands():
            if not tag:
                continue
            if cmd.started:
                anidb_client.log.debug(f"Command {tag} started at {cmd.started} (now {monotonic()})")
                if now - cmd.started > self.timeout:
                    willpop.append(tag)

        for tag in willpop:
            cmd = self.pop_command(tag)
            if cmd is None:
                # Its reply landed between the sweep above and this pop.
                continue
            # `started is None` means the command was re-queued between the sweep
            # above and this pop and has not gone out again yet, so it has not timed
            # out at all -- it belongs in the same re-request branch. Comparing it
            # raised TypeError on this thread, which ends the listener, and a
            # listener that has stopped reading the socket is a permanent hang.
            # Both branches can end up sending, and sending is refused while a
            # back-off is open. That refusal is this command's answer, not a fault
            # in the sweep -- and left to escape it would end the listener, which is
            # the failure the whole sweep exists to avoid.
            try:
                if cmd.started is None or cmd.started < self._last_receive:
                    # API isn't dead yet, probably reauthenticating
                    self._sender.request(cmd, cmd.callback, prio=True)
                else:
                    anidb_client.log.warning(f"Command {tag} timed out")
                    cmd.handle_timeout(self._sender, silenced=self._note_silent_timeout(cmd))
            except AniDBError as e:
                anidb_client.log.error(f"Giving up on {cmd.command} ({tag}): {e}")
                cmd.fail(e)

    def _note_silent_timeout(self, cmd: Command) -> bool:
        """Count a command that went unanswered, and say whether the gate is closed.

        Reached only from the branch that has already established genuine
        silence: the command went out, the timeout passed, and no datagram of any
        kind arrived after it was sent. `_note_datagram` resets the count, so
        this only ever counts a consecutive run.

        A handshake is not counted. It never retries -- an unanswered AUTH backs
        off on the first timeout, by itself -- so it needs no detector, and
        counting it here would register the same silence as a ban twice.
        """
        if cmd.command in ("AUTH", "ENCRYPT"):
            return False
        self._silent_timeouts += 1
        if self._silent_timeouts < self.SILENT_TIMEOUTS_BEFORE_BAN:
            return False
        anidb_client.log.error(
            f"{self._silent_timeouts} commands unanswered and nothing from AniDB for "
            f"{monotonic() - self._last_datagram:.0f}s; treating the silence as a ban"
        )
        return True
