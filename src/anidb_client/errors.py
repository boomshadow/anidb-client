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
    """


class AniDBPacketCorruptedError(AniDBError):
    pass


class AniDBInternalError(AniDBError):
    pass


class AniDBBannedError(AniDBError):
    pass


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
