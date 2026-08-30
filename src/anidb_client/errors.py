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

import enum


class BanCause(enum.Enum):
    """Why the transport is currently refusing to send.

    A back-off is opened for three different reasons, and a caller handed one
    can only decide what to do about it if it can tell them apart. They are not
    the same situation: one is AniDB saying no, one is AniDB saying nothing at
    all, and one never reached AniDB.
    """

    # AniDB answered with a response code whose disposition says stop.
    REFUSED = enum.auto()
    # AniDB stopped answering. Its documented enforcement is to drop packets
    # rather than to refuse them, so silence is a refusal with no reply to read.
    SILENCE = enum.auto()
    # The datagram never left this host -- a socket error, a name that would not
    # resolve. Nothing was asked of AniDB, and the back-off is self-inflicted.
    LOCAL = enum.auto()


class AniDBError(Exception):
    pass


class AniDBIncorrectParameterError(AniDBError):
    pass


class AniDBCommandTimeoutError(AniDBError):
    pass


class AniDBMustAuthError(AniDBError):
    pass


class AniDBAuthFailedError(AniDBError):
    """AniDB refused this client's credentials or identity.

    Distinct from AniDBMustAuthError, which means a command was sent before a
    session existed. This one means a session was asked for and denied, and
    denied for a reason that retrying cannot change -- a wrong password, an
    unregistered client, an encryption type the server does not offer. Re-sending
    rejected credentials is one of the surest ways to earn a ban, so the transport
    latches this and stops rather than trying again.

    Carries the response code AniDB refused with, because "the login failed" and
    "this client version is no longer registered" want different things done
    about them and a message string is not something a caller can branch on.
    """

    def __init__(self, message: str, *, rescode: str | None = None) -> None:
        super().__init__(message)
        self.rescode = rescode


class AniDBPacketCorruptedError(AniDBError):
    pass


class AniDBInternalError(AniDBError):
    pass


class AniDBBannedError(AniDBError):
    """The transport is refusing to send, and this is how long for and why.

    Distinct from AniDBAuthFailedError in the way that matters to a caller: this
    one clears on its own, so the request is worth making again once
    `retry_after` has elapsed. The other needs a human.

    The structured fields exist because the message did not survive being read
    by a program. It rounded the remaining time to whole minutes, so a fifteen
    second back-off read as "0 minutes", and it said nothing at all about which
    of the three refusals had happened -- leaving a caller to choose between
    parsing English and treating every back-off identically.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: BanCause = BanCause.REFUSED,
        retry_after: float = 0.0,
        rescode: str | None = None,
    ) -> None:
        super().__init__(message)
        # Which of the three refusals this is.
        self.cause = cause
        # Seconds until anything may be sent again, unrounded. Zero means the
        # window has already closed.
        self.retry_after = retry_after
        # The AniDB response code that closed the gate, when there was one. A
        # silent ban has none -- that is what makes it silent.
        self.rescode = rescode


class AniDBFileError(AniDBError):
    pass


class AniDBPathError(AniDBError):
    pass


class IllegalAnimeObject(AniDBError):
    pass


class FanartError(AniDBError):
    pass


class AniDBMissingImage(AniDBError):
    pass
