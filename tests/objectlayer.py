"""A link stand-in for testing the object layer.

The transport is already covered against a real loopback UDP server
(tests/integration/test_link.py). What the object layer needs tested is a
different question: *which commands does it decide to send, and what does it do
with the answers*. A recording double answers that directly, and synchronously --
which matters because several of these code paths block on a threading.Event
until their callback fires.
"""


class FakeResponse:
    """The response surface the object-layer callbacks actually touch."""

    def __init__(self, rescode, datalines=None, attrs=None):
        self.rescode = rescode
        self.datalines = datalines if datalines is not None else []
        self.attrs = attrs or {}
        self.resstr = ""
        self.restag = None


class RecordingLink:
    """Stands in for AniDBLink: records every request and replies from a script.

    Callbacks are invoked inline, before request() returns. Real replies arrive on
    the listener thread, but the object layer always waits on an Event immediately
    afterwards, so answering synchronously reaches the same state without the
    scheduling nondeterminism -- and a missing reply shows up as a test timeout
    rather than a hang.
    """

    # An unscripted request still gets an answer, because *not* answering hangs the
    # caller outright: the object layer waits on a threading.Event with no timeout,
    # and only the callback sets it. These are each command's documented
    # "not found" code, so a test that forgets to script a reply gets a clean
    # negative result instead of a deadlock.
    NOT_FOUND = {
        "ANIME": "330",
        "EPISODE": "340",
        "FILE": "320",
        "GROUP": "350",
        "MYLIST": "321",
        "MYLISTADD": "320",
        "MYLISTDEL": "411",
    }

    def __init__(self):
        self.requests = []
        self._responders = {}

    def on(self, command, response):
        """Reply to `command` with a FakeResponse, or a callable taking the command."""
        self._responders[command.upper()] = response
        return self

    def never_answers(self, command):
        """Drop replies to `command`, reproducing a request that is never answered.

        Only for tests that deliberately assert on that; anything waiting on the
        result will block until the suite timeout fires.
        """
        self._responders[command.upper()] = None
        return self

    def request(self, command, callback, prio=False):
        self.requests.append(command)
        if command.command in self._responders:
            responder = self._responders[command.command]
        else:
            code = self.NOT_FOUND.get(command.command)
            responder = FakeResponse(code) if code else None
        if responder is None:
            return
        response = responder(command) if callable(responder) else responder
        if response is not None:
            callback(response)

    # -- assertions -------------------------------------------------------

    def commands(self):
        return [c.command for c in self.requests]

    def requests_for(self, command):
        return [c for c in self.requests if c.command == command.upper()]

    def params_for(self, command):
        """The parameters each request for `command` actually puts on the wire.

        Drops the transport fields, and drops None values -- flatten() omits those,
        so including them would assert on fields that were never sent.
        """
        return [
            {k: v for k, v in c.parameters.items() if k not in ("tag", "s") and v is not None}
            for c in self.requests_for(command)
        ]

    # AniDBLink surface the object layer touches beyond request().
    def set_banned(self, code, reason=None):  # pragma: no cover - not exercised here
        pass
