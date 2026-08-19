"""A link stand-in for testing the object layer.

The transport is already covered against a real loopback UDP server
(tests/integration/test_link.py). What the object layer needs tested is a
different question: *which commands does it decide to send, and what does it do
with the answers*. A recording double answers that directly, and synchronously --
which matters because several of these code paths block until their reply
settles.

The one contract this double has to keep faithfully is the shape of that reply.
`request()` returns the command's outcome, and the outcome settles *after* the
callback has run -- both because that is what the transport guarantees, and
because the object layer relies on it: a caller released before the callback
finished would read a cache the callback had not written yet.
"""


class FakeResponse:
    """The response surface the object-layer callbacks actually touch."""

    def __init__(self, rescode, datalines=None, attrs=None, resstr=""):
        self.rescode = rescode
        self.datalines = datalines if datalines is not None else []
        self.attrs = attrs or {}
        self.resstr = resstr
        self.restag = None


class RecordingLink:
    """Stands in for AniDBLink: records every request and replies from a script.

    Callbacks are invoked inline, before request() returns, and the outcome is
    settled immediately after. Real replies arrive on the listener thread, but the
    object layer waits on the outcome either way, so answering synchronously
    reaches the same state without the scheduling nondeterminism.
    """

    # An unscripted request still gets an answer. These are each command's
    # documented "not found" code, so a test that forgets to script a reply gets a
    # clean negative result rather than sitting out the object layer's request
    # timeout -- which is bounded now, but measured in minutes.
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

        The outcome is left unsettled, exactly as the real transport leaves it
        until it gives up. A caller waiting on it now waits out its own timeout
        and is told; before, it waited forever.
        """
        self._responders[command.upper()] = None
        return self

    def fails(self, command, error):
        """Answer `command` by failing its outcome, as a ban or a timeout does.

        The path with no reply and no callback at all -- the one a recording
        double that only ever calls callbacks cannot reach, and the one the
        reported incident took.
        """
        self._responders[command.upper()] = error
        return self

    def request(self, command, callback, prio=False):
        self.requests.append(command)
        future = command.future
        if command.command in self._responders:
            responder = self._responders[command.command]
        else:
            code = self.NOT_FOUND.get(command.command)
            responder = FakeResponse(code) if code else None
        if isinstance(responder, BaseException):
            future.set_exception(responder)
            return future
        if responder is None:
            return future
        response = responder(command) if callable(responder) else responder
        if response is not None:
            callback(response)
            # After the callback, never before: see the module docstring.
            if not future.done():
                future.set_result(response)
        return future

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
