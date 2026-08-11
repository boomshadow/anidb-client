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

import logging
import logging.handlers
import netrc
import os
import random
import urllib.parse
import urllib.request

import anidb_client.db
import anidb_client.errors
from anidb_client.anames import get_titles, update_anilist, update_animetitles
from anidb_client.animeobjs import Anime, AnimeTitle, Episode, File, Group
from anidb_client.link import AniDBLink

# The library's public surface. Declared explicitly so that re-exports here are
# understood as the API rather than as unused imports, and so `from anidb_client import *`
# cannot leak internals.
__all__ = [
    "AniDBLink",
    "Anime",
    "AnimeTitle",
    "Episode",
    "File",
    "Group",
    "close",
    "close_session",
    "download_fanart",
    "download_image",
    "get_session",
    "get_titles",
    "init",
    "update_anilist",
    "update_animetitles",
]

# This distribution's own version. Read by the build backend, so it is the single
# source of truth for the released package version.
__version__ = "1.0.0"

# Identity sent to AniDB in the AUTH command. The (name, version) pair must be
# registered with AniDB before it will authenticate, and `anidb_client_version` is
# that registration's integer -- deliberately unrelated to __version__ above, which
# moves on its own semantic-versioning schedule.
anidb_client_name = "anidbclientpy"
anidb_client_version = 1
anidb_api_version = 3

log = None
_anidb = None
_sessionmaker = None
fanart_key = None


def init(
    sql_db_url,
    api_user=None,
    api_pass=None,
    debug=False,
    loglevel="info",
    logger=None,
    netrc_file=None,
    outgoing_udp_port=None,
    api_key=None,
    fanart_api_key=None,
    db_only=False,
    client_name=None,
    client_version=None,
):
    # Chosen here rather than in the signature: a call in a default argument is
    # evaluated once, when the module is imported, so every init() in a process
    # previously reused the same "random" port -- and it was baked in at import
    # time rather than chosen when the link was actually opened.
    if outgoing_udp_port is None:
        outgoing_udp_port = random.randrange(9000, 10000)

    if logger is None:
        logger = logging.getLogger(__name__)
        logger.setLevel(loglevel.upper())
        if debug:
            logger.setLevel(logging.DEBUG)
            lh = logging.StreamHandler()
            lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(filename)s:%(lineno)d - %(message)s"))
            logger.addHandler(lh)
        if os.path.exists("/dev/log"):
            lh = logging.handlers.SysLogHandler(address="/dev/log")
        else:
            lh = logging.handlers.SysLogHandler()
        lh.setFormatter(logging.Formatter("anidb_client %(filename)s/%(funcName)s:%(lineno)d - %(message)s"))
        logger.addHandler(lh)

    global log, _anidb, _sessionmaker, fanart_key
    log = logger
    fanart_key = fanart_api_key

    try:
        nrc = netrc.netrc(netrc_file)
    except FileNotFoundError:
        nrc = None

    # unless both username and password is given; look for credentials in netrc
    if not (api_user and api_pass) or db_only:
        if not nrc:
            raise Exception("User and passwords are required if no netrc file exists")
        for host in ["api.anidb.net", "api.anidb.info", "anidb.net"]:
            try:
                username, account, password = nrc.authenticators(host)
            except TypeError:
                continue
            if username and password:
                api_user = username
                api_pass = password
                if account and not api_key:
                    api_key = account
                break

    if not db_only:
        _anidb = anidb_client.link.AniDBLink(
            api_user,
            api_pass,
            myport=outgoing_udp_port,
            api_key=api_key,
            client_name=client_name,
            client_version=client_version,
        )

    if nrc:
        # if no password is given in sql-url we try to look it up
        # in netrc
        parts = sql_db_url.split("/")
        if parts[2] and ":" not in parts[2]:
            if "@" in parts[2]:
                username, host = parts[2].split("@")
            else:
                username, host = (None, parts[2])
            try:
                u, _account, password = nrc.authenticators(host)
            except TypeError:
                u, password = (None, None)
            if password:
                if not username:
                    username = u
                if username == u:
                    parts[2] = f"{username}:{password}@{host}"
        sql_db_url = "/".join(parts)

        if not fanart_key:
            for host in ["fanart.tv", "assets.fanart.tv", "webservice.fanart.tv", "api.fanart.tv"]:
                try:
                    username, account, password = nrc.authenticators(host)
                except TypeError:
                    continue
                key = [x for x in [account, password] if x]
                if not key:
                    continue
                log.debug("Fanart key found in netrc")
                fanart_key = key[0]

    _sessionmaker = anidb_client.db.init_db(sql_db_url)


def get_session():
    return _sessionmaker()


def close_session(session):
    session.close()


def download_image(filehandle, obj):
    if type(obj) not in (Anime, Group):
        raise anidb_client.errors.AniDBMissingImage(f"Object type {type(obj)} does not support images")
    if not obj.picname:
        raise anidb_client.errors.AniDBMissingImage(f"{obj} does not have a picture defined")
    url_base = "https://cdn.anidb.net/images/main"
    url = f"{url_base}/{obj.picname}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as f:
        filehandle.write(f.read())


def download_fanart(filehandle, url, preview=False):
    if not fanart_key:
        raise anidb_client.errors.FanartError("No fanart key available")
    my_url = urllib.parse.urlparse(url)
    if preview:
        my_url = urllib.parse.urlparse(url)._replace(
            scheme="https", path=urllib.parse.quote(my_url.path.replace("/fanart/", "/preview/"))
        )
    else:
        my_url = urllib.parse.urlparse(url)._replace(scheme="https", path=urllib.parse.quote(my_url.path))

    req = urllib.request.Request(my_url.geturl(), headers={"api-key": fanart_key})
    with urllib.request.urlopen(req) as f:
        filehandle.write(f.read())


def close():
    global _anidb
    if _anidb:
        _anidb.stop()
