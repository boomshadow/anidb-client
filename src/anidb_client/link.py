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
)
from anidb_client.ratelimit import RateLimiter
from anidb_client.responses import Disposition, Response, ResponseResolver, disposition_for

# The AES cipher objects pycryptodome hands back are one of several mode classes
# with no common base, and this code only ever calls encrypt/decrypt on them. Any
# rather than a union that would have to be widened for every mode never used.
type Cipher = Any


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

    def __init__(
        self,
        user: str,
        pwd: str,
        # host='localhost',
        host: str = "api.anidb.net",
        port: int = 9000,
        myport: int = 9876,
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
        if self._api_key:
            self._start_encrypted_session()
        else:
            self._send_auth()

    def _settle_auth(self, error: AniDBError | None) -> None:
        """Release whoever is waiting on the handshake, one way or the other.

        Every path that ends an authentication attempt comes through here, which
        is the whole point: an attempt that ends without settling its future is a
        sender parked on a reply that has already been and gone.
        """
        with self._auth_lock:
            attempt, self._auth_attempt = self._auth_attempt, None
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
        _ip, _sep, port = addr.rpartition(":")
        if port.isdigit() and int(port) != self._myport:
            self._do_ping = True
            anidb_client.log.info(f"NAT detected: will send PING every {self._nat_ping_interval} seconds")
        with self._auth_lock:
            self._authed.set()
            self._authenticating.clear()
        self._settle_auth(None)
        anidb_client.log.info(f"Logged in to AniDB with session {self.session}")

    def auth_failed(self, rescode: str, reason: str) -> None:
        """Report that a handshake round trip came back as anything but success.

        Called from the listener thread, which is the only one that sees the
        reply, and from a handshake command's timeout. It settles the waiting
        sender rather than leaving it on an Event that nothing will ever set.

        Whether another attempt is worth making is decided from the response
        table: a code that means the server is unhappy -- busy, down, banning us
        for now -- backs off and may be retried later. Anything else is a refusal
        of these credentials or this client identity, which no amount of retrying
        will change, so it is latched and no further AUTH is sent.
        """
        error: AniDBError
        if disposition_for(rescode) is not Disposition.NORMAL:
            error = AniDBBannedError(f"AniDB refused authentication: {rescode} {reason}")
            # register_ban() rather than set_banned(): this runs on the listener
            # thread, and set_banned() re-authenticates, which would send AUTH --
            # and pay the back-off sleep -- from the thread that has to keep
            # reading the socket. The sender re-authenticates on its next command
            # and waits out the back-off there, where waiting is free.
            self._rate_limiter.register_ban()
            anidb_client.log.error(f"Backing off: {error}")
        else:
            error = AniDBAuthFailedError(f"AniDB refused authentication and retrying will not help: {rescode} {reason}")
            anidb_client.log.error(str(error))
        with self._auth_lock:
            if isinstance(error, AniDBAuthFailedError):
                self._auth_fatal = error
            self._authed.clear()
            self._authenticating.clear()
            self._session = None
            self._listener.cipher = None
        self._settle_auth(error)

    def _await_auth(self) -> None:
        """Block until the handshake settles, raising if it settled as a failure.

        Was `self._authed.wait()` with no timeout and no failure case, which is
        the second half of the reported hang: the first half dropped the reply,
        this half waited for it forever.
        """
        deadline = monotonic() + self.timeout * self.AUTH_TIMEOUT_FACTOR
        while True:
            with self._auth_lock:
                if self._auth_fatal is not None:
                    raise self._auth_fatal
                if self._authed.is_set():
                    return
                attempt = self._auth_attempt
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AniDBCommandTimeoutError("Timed out waiting for authentication")
            if attempt is None:
                # Nothing in flight and not authenticated: an attempt settled
                # without authenticating us. Waiting longer cannot change that.
                raise AniDBMustAuthError("Authentication did not complete")
            # Raises whatever failed the attempt, or returns and the loop above
            # confirms the session really is up before any command goes out.
            attempt.result(timeout=remaining)

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
        if self._do_ping and time_since_cmd > self._nat_ping_interval:
            self.request(anidb_client.commands.PingCommand(), self._ping_callback)
        elif time_since_cmd >= 1800:
            anidb_client.log.debug("Session idle for 30 minutes, sending UPTIME command")
            self.request(anidb_client.commands.UptimeCommand(), self._ping_callback)

    def run(self) -> None:
        while True:
            command = self._take_next_command()
            if command is None:
                self._send_idle_keepalive()
                continue

            anidb_client.log.debug(f"sending command {command.command} with tag {command.tag}")
            if not (self._authed.is_set() or command.command in ("AUTH", "ENCRYPT", "PING")):
                self.reauthenticate()
                try:
                    self._await_auth()
                except AniDBAuthFailedError as e:
                    # No session is ever coming.
                    anidb_client.log.error(f"Dropping {command.command} ({command.tag}): {e}")
                    self._fail_command(command, e)
                    if command.command == "LOGOUT":
                        break
                    continue
                except AniDBError as e:
                    # The handshake may still work later -- the API was busy, or
                    # did not answer. Put the command back rather than losing it;
                    # it stays registered under the same tag, and the back-off
                    # registered by the failure decides when the next attempt goes
                    # out. There is nothing to log out of if we never got in.
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
            self.set_banned(code=999, reason=b"Network unavailable")

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

    def reauthenticate(self) -> None:
        # One critical section: a half-cleared state -- session gone but cipher
        # still set, or the reverse -- is a command encrypted with a key the server
        # has forgotten, or an unencrypted command on a session that requires one.
        with self._auth_lock:
            self._authed.clear()
            self._session = None
            self._listener.cipher = None
        self._reauthenticate()

    def stop(self) -> None:
        if self._authed.is_set():
            anidb_client.log.debug("Logging out from AniDB")
            req = anidb_client.commands.LogoutCommand()
            self.request(req, self._logout_handler)
            self._stop.wait(self.timeout)
        else:
            self._listener.stop()

    def set_banned(self, code: int, reason: bytes | str | None = None) -> None:
        # Decoded rather than interpolated: the reasons raised from commands.py are
        # bytes literals, which formatted as b'API not responding' in the log line.
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        anidb_client.log.error(f"Backing off: {reason}")
        self._rate_limiter.register_ban()
        with self._auth_lock:
            self._authenticating.clear()
        self.reauthenticate()


class AniDBListener(threading.Thread):
    def __init__(self, sender: AniDBLink, myport: int = 9876, timeout: int = 20) -> None:
        super().__init__()

        self.timeout = timeout
        self.sock: socket.socket | None = self._connect_socket(myport, self.timeout)
        self._sender = sender
        # Written by whichever thread completes an ENCRYPT or drops the session,
        # read by this thread on every packet. Behind an accessor rather than being
        # reached into from AniDBLink, which is what it was.
        self._cipher_lock = threading.Lock()
        self._cipher: Cipher | None = None
        self._last_receive = monotonic()
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
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", myport))
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
        anidb_client.log.debug("Closing listening socket")
        # Signalled before the socket is closed so the loop below can tell a
        # deliberate shutdown from a transient socket error.
        self._stopping.set()
        self._disconnect_socket()

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
            if resolved.restag:
                cmd = self.pop_command(resolved.restag)
                if cmd is None:
                    continue
            else:
                # No responsetag... we're probably banned
                #
                # The verdict comes from the response table in responses.py,
                # which is where AniDB's contract is transcribed. It used to be
                # the literal tuple (600, 601, 602, 604) here -- and `555 BANNED`,
                # the code AniDB actually answers with when it has had enough of
                # a client, was in the table and not in the tuple. So the one
                # reply that says "stop" was logged as unrecognised and the
                # client carried on sending.
                code = resolved.rescode
                reason = resolved.resstr
                if (disposition := disposition_for(code)) is not Disposition.NORMAL:
                    anidb_client.log.warning(f"API says {code} {reason} ({disposition.name})")
                    self._sender.set_banned(code=int(code), reason=reason)
                elif code == "598":
                    # We get here if an encrypted session has timed out
                    # No need to log in again if all that's left in queue is a
                    # logout command.
                    if all(x.command == "LOGOUT" for _tag, x in self.pending_commands()):
                        self.stop()
                    else:
                        anidb_client.log.warning("Lost encrypted session with AniDB; attempting to reauthenticate")
                        self._sender.reauthenticate()
                else:
                    # Also previously sys.exit(2); see above. An untagged reply we
                    # do not recognise is worth shouting about, but it is not worth
                    # killing the caller's process over.
                    anidb_client.log.error(f"Unhandled response from API: {repr(data)}")
                self._last_receive = monotonic()
                continue
            resp = resolved.resolve(cmd)
            resp.parse()
            if cmd.command in ("AUTH", "ENCRYPT") and not self._is_successful_handshake(cmd, resp):
                self._last_receive = monotonic()
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
                    self._sender.reauthenticate()
                    self._sender.request(cmd, cmd.callback, prio=True)
                self._last_receive = monotonic()
                continue
            elif resp.rescode in ("203", "500", "503"):
                self.stop()

            self._last_receive = monotonic()
            resp_thread = threading.Thread(target=self._deliver, args=(cmd, resp))
            resp_thread.daemon = True
            resp_thread.start()

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
            if cmd.started is None or cmd.started < self._last_receive:
                # API isn't dead yet, probably reauthenticating
                self._sender.request(cmd, cmd.callback, prio=True)
            else:
                anidb_client.log.warning(f"Command {tag} timed out")
                cmd.handle_timeout(self._sender)
