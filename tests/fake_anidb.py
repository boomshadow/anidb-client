"""A scripted stand-in for the AniDB UDP API, on loopback.

This is a real UDP socket, not a mock. That matters: the transport's genuinely
tricky parts are the parts a mock would skip -- binding, correlating replies to
commands by tag, deflate-compressed payloads, AES-ECB encrypted sessions, and the
timeout path. Those are exercised here for real, over 127.0.0.1.

It also makes testable the scenarios that must never be provoked against the real
service: bans, server-busy replies, session expiry, and malformed packets.

Usage::

    with FakeAniDBServer() as server:
        server.on("AUTH", "200 sess1234 127.0.0.1:9000 LOGIN ACCEPTED")
        link = AniDBLink("user", "pw", host=server.host, port=server.port, ...)
"""

import socket
import threading
import zlib


class FakeAniDBServer:
    """A UDP server that answers commands from a scripted table.

    Handlers are registered per command verb. A handler is either a literal reply
    body (the tag is prefixed automatically) or a callable taking the parsed
    request and returning a body, None to stay silent, or bytes to send verbatim.
    """

    def __init__(self, host="127.0.0.1"):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, 0))
        self._sock.settimeout(0.2)
        self.host, self.port = self._sock.getsockname()

        self._handlers = {}
        self._default = None
        self._running = threading.Event()
        self._thread = None

        # Everything received, in order, for assertions after the fact.
        self.requests = []
        self._lock = threading.Lock()

        # Set to an AES cipher factory to encrypt replies, mirroring ENCRYPT.
        self.cipher = None
        # When true, replies are deflate-compressed with the 2-byte marker.
        self.compress = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        self._running.set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=5)
        self._sock.close()

    # -- scripting ---------------------------------------------------------

    def on(self, verb, reply):
        """Answer `verb` with `reply` (a body string, bytes, or a callable)."""
        self._handlers[verb.upper()] = reply
        return self

    def default(self, reply):
        """Answer any unscripted command with `reply`."""
        self._default = reply
        return self

    def requests_for(self, verb):
        with self._lock:
            return [r for r in self.requests if r["command"] == verb.upper()]

    def wait_for(self, verb, timeout=5.0):
        """Block until `verb` has been received at least once; return it."""
        deadline = threading.Event()
        waited = 0.0
        step = 0.02
        while waited < timeout:
            matches = self.requests_for(verb)
            if matches:
                return matches[0]
            deadline.wait(step)
            waited += step
        raise AssertionError(f"fake server never received a {verb} command; got {self.received_commands()}")

    def received_commands(self):
        with self._lock:
            return [r["command"] for r in self.requests]

    # -- internals ---------------------------------------------------------

    def _serve(self):
        while self._running.is_set():
            try:
                data, addr = self._sock.recvfrom(8192)
            except TimeoutError:
                continue
            except OSError:
                break

            request = self._parse(data)
            # The client's source address, as the real server would see it. AUTH
            # replies echo it back, which is how NAT detection works.
            request["peer"] = addr
            with self._lock:
                self.requests.append(request)

            handler = self._handlers.get(request["command"], self._default)
            if handler is None:
                continue

            reply = handler(request) if callable(handler) else handler
            if reply is None:
                continue
            if isinstance(reply, bytes):
                self._sock.sendto(reply, addr)
                continue

            # A tagged request gets a tagged reply; that is how the client
            # correlates it. Untagged replies (bans) are sent by returning bytes.
            body = f"{request['tag']} {reply}\n" if request["tag"] else f"{reply}\n"
            self._sock.sendto(self._encode(body), addr)

    def _parse(self, data):
        if self.cipher is not None:
            data = self._decrypt(data)
        text = data.decode("utf-8", "replace")
        verb, _, fieldstr = text.partition(" ")
        fields = {}
        for pair in fieldstr.split("&"):
            key, _, value = pair.partition("=")
            if key:
                fields[key.strip()] = value
        return {
            "raw": text,
            "command": verb.strip().upper(),
            "fields": fields,
            "tag": fields.get("tag"),
        }

    def _encode(self, body):
        data = body.encode("utf-8")
        if self.compress:
            # Two zero bytes, then a zlib stream *with* its header: the client
            # decodes with a default decompressobj(), which expects one.
            data = b"\x00\x00" + zlib.compress(data)
        if self.cipher is not None:
            data = self._encrypt(data)
        return data

    def _encrypt(self, data):
        pad = 16 - len(data) % 16
        return self.cipher().encrypt(data + bytes([pad]) * pad)

    def _decrypt(self, data):
        plain = self.cipher().decrypt(data)
        return plain[: -plain[-1]]
