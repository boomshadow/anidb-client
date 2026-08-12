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
from time import monotonic

from Crypto.Cipher import AES

import anidb_client.commands
from anidb_client.errors import AniDBInternalError, AniDBMustAuthError
from anidb_client.ratelimit import RateLimiter
from anidb_client.responses import ResponseResolver


class AniDBLink(threading.Thread):
    # How long the sender waits on an empty queue before checking whether the
    # session needs a keepalive.
    IDLE_TICK = 0.2

    def __init__(
        self,
        user,
        pwd,
        # host='localhost',
        host="api.anidb.net",
        port=9000,
        myport=9876,
        nat_ping_interval=600,
        timeout=20,
        api_key=None,
        client_name=None,
        client_version=None,
        rate_limiter=None,
    ):
        super().__init__()
        self._user = user
        self._pwd = pwd
        # Identity sent in AUTH. Held per-link rather than read from the module
        # globals at send time, so an application embedding this library can
        # authenticate as its own registered client without mutating global state.
        self._client_name = client_name if client_name is not None else anidb_client.anidb_client_name
        self._client_version = client_version if client_version is not None else anidb_client.anidb_client_version
        self._server = (host, port)
        self._queue = deque()
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
        self._session = None

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

    def _logout_handler(self, resp):
        anidb_client.log.info("Logged out from AniDB")
        self._stop.set()

    def _start_encrypted_session(self):
        req = anidb_client.commands.EncryptCommand(self._user, self._api_key, "1")
        self.request(req, self._encryption_handler)

    def _encryption_handler(self, resp):
        self._session_key = hashlib.md5(bytes(self._api_key + resp.attrs["salt"], "utf-8")).digest()
        self._listener.cipher = AES.new(self._session_key, AES.MODE_ECB)
        anidb_client.log.info("Encrypted session established")
        self._rate_limiter.clear_ban()
        self._send_auth()

    def _send_auth(self):
        if self._api_key and not self._listener.cipher:
            anidb_client.log.error("Tried to do unencrypted auth but API Key is set!")
            return
        req = anidb_client.commands.AuthCommand(
            self._user, self._pwd, anidb_client.anidb_api_version, self._client_name, self._client_version, nat=1
        )
        self.request(req, self._auth_handler)

    def _reauthenticate(self):
        with self._auth_lock:
            if self._authenticating.is_set() or self._authed.is_set():
                return
            self._authenticating.set()
        if self._api_key:
            self._start_encrypted_session()
        else:
            self._send_auth()

    def _auth_handler(self, resp):
        # Authentication succeeded, so whatever the back-off was for has passed.
        self._rate_limiter.clear_ban()
        addr = resp.attrs["address"]
        ip, port = addr.split(":")
        port = int(port)
        if port != self._myport:
            self._do_ping = True
            anidb_client.log.info(f"NAT detected: will send PING every {self._nat_ping_interval} seconds")
        with self._auth_lock:
            self._authed.set()
            self._authenticating.clear()
        anidb_client.log.info(f"Logged in to AniDB with session {self.session}")

    def _new_tag(self):
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

    def _ping_callback(self, _resp):
        anidb_client.log.debug("Successful session refresh")

    def _enqueue(self, command, prio=False):
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

    def _take_next_command(self):
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

    def _send_idle_keepalive(self):
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

    def run(self):
        while True:
            command = self._take_next_command()
            if command is None:
                self._send_idle_keepalive()
                continue

            anidb_client.log.debug(f"sending command {command.command} with tag {command.tag}")
            if self._authed.is_set() or command.command in ("AUTH", "ENCRYPT", "PING"):
                self._send_command(command)
            else:
                self.reauthenticate()
                self._authed.wait()
                self._send_command(command)

            if command.command == "LOGOUT":
                break

    def _send_command(self, command):
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

    def request(self, command, callback, prio=False):
        command.started = None
        command.callback = callback
        command.tag = self._new_tag()
        self._listener.queue_command(command)
        anidb_client.log.debug(f"Queued command {command.command} with tag {command.tag}")
        if command.command in ("ENCRYPT", "AUTH", "PING"):
            self._send_command(command)
            return
        self._enqueue(command, prio=prio)

    @property
    def session(self):
        """The current session key, or None.

        Read through the lock because it is written by the listener thread (on a
        successful AUTH, and cleared on a lost session) and read by the sender
        thread on every command. Callers that both test it and use it must read it
        once through here rather than touching `_session` twice.
        """
        with self._auth_lock:
            return self._session

    def set_session(self, session):
        with self._auth_lock:
            self._session = session

    def reauthenticate(self):
        # One critical section: a half-cleared state -- session gone but cipher
        # still set, or the reverse -- is a command encrypted with a key the server
        # has forgotten, or an unencrypted command on a session that requires one.
        with self._auth_lock:
            self._authed.clear()
            self._session = None
            self._listener.cipher = None
        self._reauthenticate()

    def stop(self):
        if self._authed.is_set():
            anidb_client.log.debug("Logging out from AniDB")
            req = anidb_client.commands.LogoutCommand()
            self.request(req, self._logout_handler)
            self._stop.wait(self.timeout)
        else:
            self._listener.stop()

    def set_banned(self, code, reason=None):
        anidb_client.log.error(f"Backing off: {reason}")
        self._rate_limiter.register_ban()
        with self._auth_lock:
            self._authenticating.clear()
        self.reauthenticate()


class AniDBListener(threading.Thread):
    def __init__(self, sender, myport=9876, timeout=20):
        super().__init__()

        self.timeout = timeout
        self.sock = self._connect_socket(myport, self.timeout)
        self._sender = sender
        # Written by whichever thread completes an ENCRYPT or drops the session,
        # read by this thread on every packet. Behind an accessor rather than being
        # reached into from AniDBLink, which is what it was.
        self._cipher_lock = threading.Lock()
        self._cipher = None
        self._last_receive = monotonic()
        self._stopping = threading.Event()

        self.cmd_queue = {}
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
    def cipher(self):
        with self._cipher_lock:
            return self._cipher

    @cipher.setter
    def cipher(self, value):
        with self._cipher_lock:
            self._cipher = value

    def queue_command(self, command):
        """Register a command so its reply can be matched back to it."""
        with self._queue_lock:
            self.cmd_queue[command.tag] = command

    def pop_command(self, tag):
        """Claim the command awaiting `tag`, or None if nothing is waiting.

        Atomic on purpose: the callers used to test membership and then pop as two
        steps, which the timeout sweep running on this same thread could interleave.
        """
        with self._queue_lock:
            return self.cmd_queue.pop(tag, None)

    def pending_commands(self):
        """A snapshot of the outstanding (tag, command) pairs, safe to iterate."""
        with self._queue_lock:
            return list(self.cmd_queue.items())

    def _connect_socket(self, myport, timeout):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", myport))
        return sock

    def _disconnect_socket(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def encrypt(self, data, cipher):
        # The cipher is passed in rather than read here, so the caller's `if cipher`
        # test and this use are the same read. Reading it again would let the
        # session drop in between -- and unlike the receive path, which can suppress
        # the failure and treat the packet as plaintext, there is nothing sensible to
        # do about it here except end the sender thread. So the race is removed
        # rather than handled.
        pad_len = 16 - len(data) % 16
        padding = (chr(pad_len) * pad_len).encode("utf-8")
        data = data + padding
        return cipher.encrypt(data)

    def decrypt(self, data):
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

    def stop(self):
        anidb_client.log.debug("Closing listening socket")
        # Signalled before the socket is closed so the loop below can tell a
        # deliberate shutdown from a transient socket error.
        self._stopping.set()
        self._disconnect_socket()

    def run(self):
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
                resp = ResponseResolver(payload)
            except (UnicodeDecodeError, ValueError) as e:
                anidb_client.log.warning(f"Unparsable response from API ({e}): {repr(data)}")
                continue
            if resp.restag:
                cmd = self.pop_command(resp.restag)
                if cmd is None:
                    continue
            else:
                # No responsetag... we're probably banned
                try:
                    code = int(payload[:3])
                except ValueError:
                    # Previously sys.exit(2). A library must not terminate its
                    # host process, and in a non-main thread sys.exit only ends
                    # that thread -- so the listener died silently and every
                    # later command timed out with no indication why.
                    anidb_client.log.error(f"Unparsable response from API: {repr(data)}")
                    self._last_receive = monotonic()
                    continue
                reason = resp.resstr
                if code in (600, 601, 602, 604):
                    self._sender.set_banned(code=code, reason=reason)
                elif code == 598:
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
            resp = resp.resolve(cmd)
            resp.parse()
            if resp.rescode in ("200", "201"):
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
            resp_thread = threading.Thread(target=resp.handle)
            resp_thread.daemon = True
            resp_thread.start()

    def _handle_timeouts(self):
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
            if cmd.started < self._last_receive:
                # API isn't dead yet, probably reauthenticating
                self._sender.request(cmd, cmd.callback, prio=True)
            else:
                anidb_client.log.warning(f"Command {tag} timed out")
                cmd.handle_timeout(self._sender)
