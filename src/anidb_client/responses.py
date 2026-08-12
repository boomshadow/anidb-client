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

import anidb_client.mapper


class ResponseResolver:
    def __init__(self, data):
        data = data.decode("utf-8")
        restag, rescode, resstr, datalines = self.parse(data)

        self.restag = restag
        self.rescode = rescode
        self.resstr = resstr
        self.datalines = datalines

    def parse(self, data):
        resline = data.split("\n", 1)[0]
        lines = data.split("\n")[1:-1]

        rescode, resstr = resline.split(" ", 1)
        if rescode[0] == "T":
            restag = rescode
            rescode, resstr = resstr.split(" ", 1)
        else:
            restag = None

        datalines = []
        for line in lines:
            datalines.append(line.split("|"))

        return restag, rescode, resstr, datalines

    def resolve(self, cmd):
        return responses[self.rescode](cmd, self.restag, self.rescode, self.resstr, self.datalines)


class Response:
    """One AniDB reply, resolved to the class registered for its response code.

    The four `code*` values below describe the reply's shape: its symbolic name,
    and the field names for the leading, trailing and repeating parts of its data
    lines. Subclasses set them as **class attributes**. They used to be assigned in
    each subclass's `__init__`, which meant 104 near-identical constructors whose
    only content was four assignments -- and which made the values invisible to
    `getattr` on the class, so nothing could check them without instantiating a
    reply and its matching command.

    Defaults here so a subclass only states what it differs in, and so reading any
    of the four is safe on any response.
    """

    # Annotated because the empty defaults would otherwise infer as `tuple[()]`,
    # and every subclass's real field list would be an incompatible override.
    codestr: str = ""
    codehead: tuple[str, ...] = ()
    codetail: tuple[str, ...] = ()
    coderep: tuple[str, ...] = ()

    def __init__(self, cmd, restag, rescode, resstr, rawlines):
        self.req = cmd
        self.restag = restag
        self.rescode = rescode
        self.resstr = resstr
        self.rawlines = rawlines

    def __repr__(self):
        tmp = f"{self.__class__.__name__}({self.restag!r},{self.rescode!r},{self.resstr!r}) {self.attrs!r}\n"

        m = 0
        for line in self.datalines:
            for k in line:
                if len(k) > m:
                    m = len(k)

        for line in self.datalines:
            tmp += "  Line:\n"
            for k, v in line.items():
                tmp += "    {}:{} {}\n".format(k, (m - len(k)) * " ", v)
        return tmp

    def parse(self):
        tmp = self.resstr.split(" ", len(self.codehead))
        # strict=False throughout: the code* tuples are deliberately shorter than
        # the payload, which is how optional trailing fields are ignored.
        self.attrs = dict(zip(self.codehead, tmp[:-1], strict=False))
        self.resstr = tmp[-1]

        self.datalines = []
        for rawline in self.rawlines:
            normal = dict(zip(self.codetail, rawline, strict=False))
            rawline = rawline[len(self.codetail) :]
            rep = []
            if len(self.coderep):
                while rawline:
                    tmp = dict(zip(self.coderep, rawline, strict=False))
                    rawline = rawline[len(self.coderep) :]
                    rep.append(tmp)
            # normal['rep']=rep
            self.datalines.append(normal)

    def handle(self):
        if self.req:
            self.req.handle(self)


class CachedResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, data):
        self.datalines = [data]
        Response.__init__(self, cmd, restag, rescode, resstr, self.datalines)
        self.codestr = "CACHED"
        self.codetail = ()
        self.coderep = ()
        self.codehead = ()

    def parse(self):
        pass

    def handle(self):
        pass


class LoginAcceptedResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, datalines):
        """
        attributes:
        sesskey    - session key
        address    - your address (ip:port) as seen by the server

        data:

        """
        Response.__init__(self, cmd, restag, rescode, resstr, datalines)
        self.codestr = "LOGIN_ACCEPTED"
        self.codetail = ()
        self.coderep = ()

        nat = cmd.parameters["nat"]
        if nat in ("1", 1):
            self.codehead = ("sesskey", "address")
        else:
            self.codehead = ("sesskey",)


class LoginAcceptedNewVerResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, datalines):
        """
        attributes:
        sesskey    - session key
        address    - your address (ip:port) as seen by the server

        data:

        """
        Response.__init__(self, cmd, restag, rescode, resstr, datalines)
        self.codestr = "LOGIN_ACCEPTED_NEW_VER"
        self.codetail = ()
        self.coderep = ()

        # Identical to LoginAcceptedResponse above -- 200 and 201 differ only in
        # whether a newer client version exists, not in what the reply carries.
        #
        # This used to read `int(nat is None and nat or "0")`, which evaluates to
        # 0 for every possible input: the `is None` is inverted, and the and/or
        # chain collapses to "0" regardless. So `address` was never parsed, and
        # since link.py treats 201 as a successful login, _auth_handler then read
        # attrs["address"], raised KeyError on the response thread, and never set
        # the authenticated event -- hanging every command queued behind it.
        nat = cmd.parameters["nat"]
        if nat in ("1", 1):
            self.codehead = ("sesskey", "address")
        else:
            self.codehead = ("sesskey",)


class LoggedOutResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "LOGGED_OUT"
    codehead = ()
    codetail = ()
    coderep = ()


class ResourceResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "RESOURCE"
    codehead = ()
    codetail = ()
    coderep = ()


class StatsResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "STATS"
    codehead = ()
    codetail = ()
    coderep = ()


class TopResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "TOP"
    codehead = ()
    codetail = ()
    coderep = ()


class UptimeResponse(Response):
    """
    attributes:

    data:
    uptime    - udpserver uptime in milliseconds
    """

    codestr = "UPTIME"
    codehead = ()
    codetail = ("uptime",)
    coderep = ()


class EncryptionEnabledResponse(Response):
    """
    attributes:
    salt    - salt

    data:
    """

    codestr = "ENCRYPTION_ENABLED"
    codehead = ("salt",)
    codetail = ()
    coderep = ()


class MylistEntryAddedResponse(Response):
    """
    attributes:

    data:
    entrycnt - number of entries added
    """

    codestr = "MYLIST_ENTRY_ADDED"
    codehead = ()
    codetail = ("entrycnt",)
    coderep = ()


class MylistEntryDeletedResponse(Response):
    """
    attributes:

    data:
    entrycnt - number of entries
    """

    codestr = "MYLIST_ENTRY_DELETED"
    codehead = ()
    codetail = ("entrycnt",)
    coderep = ()


class AddedFileResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ADDED_FILE"
    codehead = ()
    codetail = ()
    coderep = ()


class AddedStreamResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ADDED_STREAM"
    codehead = ()
    codetail = ()
    coderep = ()


class EncodingChangedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ENCODING_CHANGED"
    codehead = ()
    codetail = ()
    coderep = ()


class FileResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, datalines):
        """
        attributes:

        data:
        eid        episode id
        gid        group id
        lid        mylist id
        state        state
        size        size
        ed2k        ed2k
        md5        md5
        sha1        sha1
        crc32        crc32
        dublang        dub language
        sublang        sub language
        quality        quality
        source        source
        audiocodec    audio codec
        audiobitrate    audio bitrate
        videocodec    video codec
        videobitrate    video bitrate
        resolution    video resolution
        filetype    file type (extension)
        length        length in seconds
        description    description
        filename    anidb file name
        gname        group name
        gshortname    group short name
        epno        number of episode
        epname        ep english name
        epromaji    ep romaji name
        epkanji        ep kanji name
        totaleps    anime total episodes
        lastep        last episode nr (highest, not special)
        year        year
        type        type
        romaji        romaji name
        kanji        kanji name
        name        english name
        othername    other name
        shortnames    short name list
        synonyms    synonym list
        categories    category list
        relatedaids    related aid list
        producernames    producer name list
        producerids    producer id list

        """
        Response.__init__(self, cmd, restag, rescode, resstr, datalines)
        self.codestr = "FILE"
        self.codehead = ()
        self.coderep = ()

        fmask = cmd.parameters["fmask"]
        amask = cmd.parameters["amask"]

        codeListF = anidb_client.mapper.getFileCodesF(fmask)
        codeListA = anidb_client.mapper.getFileCodesA(amask)
        # print "File - codelistF: "+str(codeListF)
        # print "File - codelistA: "+str(codeListA)

        self.codetail = tuple(["fid"] + codeListF + codeListA)


class MylistResponse(Response):
    """
    attributes:

    data:
    lid     - mylist id
    fid     - file id
    eid     - episode id
    aid     - anime id
    gid     - group id
    date     - date when you added this to mylist
    state     - the location of the file
    viewdate - date when you marked this watched
    storage     - for example the title of the cd you have this on
    source     - where you got the file (bittorrent,dc++,ed2k,...)
    other     - other data regarding this file
    filestate - the condition of the file (original, corrupted, self-edited, ...)
    """

    codestr = "MYLIST"
    codehead = ()
    codetail = (
        "lid",
        "fid",
        "eid",
        "aid",
        "gid",
        "date",
        "mylist_state",
        "mylist_viewdate",
        "mylist_storage",
        "mylist_source",
        "mylist_other",
        # The reply's last field, and previously unnamed here -- so the file
        # state arrived, was never given a name, and was dropped, even though
        # FileTable has a column for it and MYLISTADD already writes one.
        "mylist_filestate",
    )
    coderep = ()


class MylistStatsResponse(Response):
    """
    attributes:

    data:
    animes        - animes
    eps        - eps
    files        - files
    filesizes    - size of files
    animesadded    - added animes
    epsadded    - added eps
    filesadded    - added files
    groupsadded    - added groups
    leechperc    - leech %
    lameperc    - lame %
    viewedofdb    - viewed % of db
    mylistofdb    - mylist % of db
    viewedofmylist    - viewed % of mylist
    viewedeps    - number of viewed eps
    votes        - votes
    reviews        - reviews
    """

    codestr = "MYLIST_STATS"
    codehead = ()
    codetail = (
        "animes",
        "eps",
        "files",
        "filesizes",
        "animesadded",
        "epsadded",
        "filesadded",
        "groupsadded",
        "leechperc",
        "lameperc",
        "viewedofdb",
        "mylistofdb",
        "viewedofmylist",
        "viewedeps",
        "votes",
        "reviews",
    )
    coderep = ()


class AnimeResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, datalines):
        Response.__init__(self, cmd, restag, rescode, resstr, datalines)
        self.codestr = "ANIME"
        self.codehead = ()
        self.coderep = ()

        # TODO: impl random anime
        amask = cmd.parameters["amask"]
        codeList = anidb_client.mapper.getAnimeCodesA(amask)
        self.codetail = tuple(codeList)


class AnimeBestMatchResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ANIME_BEST_MATCH"
    codehead = ()
    codetail = ()
    coderep = ()


class RandomanimeResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "RANDOMANIME"
    codehead = ()
    codetail = ()
    coderep = ()


class EpisodeResponse(Response):
    """
    attributes:

    data:
    eid    - episode id
    aid    - anime id
    length    - length
    rating    - rating
    votes    - votes
    epno    - number of episode
    name    - english name of episode
    romaji    - romaji name of episode
    kanji    - kanji name of episode
    """

    codestr = "EPISODE"
    codehead = ()
    codetail = (
        "eid",
        "aid",
        "length",
        "rating",
        "votes",
        "epno",
        "title_eng",
        "title_romaji",
        "title_kanji",
        "aired",
        "type",
    )
    coderep = ()


class ProducerResponse(Response):
    """
    attributes:

    data:
    pid      - producer id
    name      - name of producer
    shortname - short name
    othername - other name
    type      - type
    pic      - picture name
    url      - home page url
    """

    codestr = "PRODUCER"
    codehead = ()
    codetail = ("pid", "name", "shortname", "othername", "type", "pic", "url")
    coderep = ()


class GroupResponse(Response):
    """
    attributes:

    data:
    gid       - group id
    rating       - rating
    votes       - votes
    animes       - anime count
    files       - file count
    name       - name
    shortname  - short
    ircchannel - irc channel
    ircserver  - irc server
    url       - url
    """

    codestr = "GROUP"
    codehead = ()
    codetail = (
        "gid",
        "rating",
        "votes",
        "acount",
        "fcount",
        "name",
        "short",
        "irc_channel",
        "irc_server",
        "url",
        "picname",
        "founded",
        "disbanded",
        "dateflag",
        "last_release",
        "last_activity",
        "relations",
    )
    coderep = ()


class GroupstatusResponse(Response):
    """
    attributes:

    data:
    gid       - group id
    rating       - rating
    votes       - votes
    animes       - anime count
    files       - file count
    name       - name
    shortname  - short
    ircchannel - irc channel
    ircserver  - irc server
    url       - url
    """

    codestr = "GROUPSTATUS"
    codehead = ()
    codetail = ("gid", "name", "state", "last_episode_number", "rating", "votes", "episode_range")
    coderep = ()


class BuddyListResponse(Response):
    """
    attributes:
    start    - mylist entry number of first buddy on this packet
    end    - mylist entry number of last buddy on this packet
    total    - total number of buddies on mylist

    data:
    uid    - uid
    name    - username
    state    - state
    """

    codestr = "BUDDY_LIST"
    codehead = ("start", "end", "total")
    codetail = ("uid", "username", "state")
    coderep = ()


class BuddyStateResponse(Response):
    """
    attributes:
    start    - mylist entry number of first buddy on this packet
    end    - mylist entry number of last buddy on this packet
    total    - total number of buddies on mylist

    data:
    uid    - uid
    state    - online state
    """

    codestr = "BUDDY_STATE"
    codehead = ("start", "end", "total")
    codetail = ("uid", "state")
    coderep = ()


class BuddyAddedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_ADDED"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyDeletedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_DELETED"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyAcceptedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_ACCEPTED"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyDeniedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_DENIED"
    codehead = ()
    codetail = ()
    coderep = ()


class VotedResponse(Response):
    """
    attributes:

    data:
    name    - aname/ename/gname
    """

    codestr = "VOTED"
    codehead = ()
    codetail = ("name",)
    coderep = ()


class VoteFoundResponse(Response):
    """
    attributes:

    data:
    name    - aname/ename/gname
    value    - vote value
    """

    codestr = "VOTE_FOUND"
    codehead = ()
    codetail = ("name", "value")
    coderep = ()


class VoteUpdatedResponse(Response):
    """
    attributes:

    data:
    name    - aname/ename/gname
    value    - vote value
    """

    codestr = "VOTE_UPDATED"
    codehead = ()
    codetail = ("name", "value")
    coderep = ()


class VoteRevokedResponse(Response):
    """
    attributes:

    data:
    name    - aname/ename/gname
    value    - vote value
    """

    codestr = "VOTE_REVOKED"
    codehead = ()
    codetail = ("name", "value")
    coderep = ()


class NotificationAddedResponse(Response):
    """
    attributes:

    data:
    nid - notofication id
    """

    codestr = "NOTIFICATION_ITEM_ADDED"
    codehead = ()
    codetail = ("nid",)
    coderep = ()


class NotificationUpdatedResponse(Response):
    """
    attributes:

    data:
    nid - notofication id
    """

    codestr = "NOTIFICATION_ITEM_UPDATED"
    codehead = ()
    codetail = ("nid",)
    coderep = ()


class NotificationEnabledResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NOTIFICATION_ENABLED"
    codehead = ()
    codetail = ()
    coderep = ()


class NotificationNotifyResponse(Response):
    """
    attributes:
    nid    - notify packet id

    data:
    aid    - anime id
    date    - date
    count    - count
    name    - name of the anime
    """

    codestr = "NOTIFICATION_NOTIFY"
    codehead = ("nid",)
    codetail = ("aid", "date", "count", "name")
    coderep = ()


class NotificationMessageResponse(Response):
    """
    attributes:
    nid    - notify packet id

    data:
    type    - type
    date    - date
    uid    - user id of the sender
    name    - name of the sender
    subject    - subject
    """

    codestr = "NOTIFICATION_MESSAGE"
    codehead = ("nid",)
    codetail = ("type", "date", "uid", "name", "subject")
    coderep = ()


class NotificationBuddyResponse(Response):
    """
    attributes:
    nid    - notify packet id

    data:
    uid    - buddy uid
    type    - event type
    """

    codestr = "NOTIFICATION_BUDDY"
    codehead = ("notify_packet_id",)
    codetail = ("uid", "type")
    coderep = ()


class NotificationShutdownResponse(Response):
    """
    attributes:
    nid    - notify packet id

    data:
    time    - time offline
    comment    - comment
    """

    codestr = "NOTIFICATION_SHUTDOWN"
    codehead = ("nid",)
    codetail = ("time", "comment")
    coderep = ()


class PushackConfirmedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "PUSHACK_CONFIRMED"
    codehead = ()
    codetail = ()
    coderep = ()


class NotifyackSuccessfulMResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NOTIFYACK_SUCCESSFUL_M"
    codehead = ()
    codetail = ()
    coderep = ()


class NotifyackSuccessfulNResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NOTIFYACK_SUCCESSFUL_N"
    codehead = ()
    codetail = ()
    coderep = ()


class NotificationResponse(Response):
    def __init__(self, cmd, restag, rescode, resstr, datalines):
        """
        attributes:

        data:
        notifies - pending notifies
        msgs     - pending msgs
        buddys     - number of online buddys

        """
        Response.__init__(self, cmd, restag, rescode, resstr, datalines)
        self.codestr = "NOTIFICATION"
        self.codehead = ()
        self.coderep = ()

        # `buddy` is a username, so the previous `int(buddy is not None and buddy
        # or "0")` reached int("someuser") and raised ValueError while the response
        # object was still being built -- on the response thread, leaving the
        # caller waiting. Only its presence matters here, not its value.
        buddy = cmd.parameters["buddy"]
        if buddy:
            self.codetail = ("notifies", "msgs", "buddys")
        else:
            self.codetail = ("notifies", "msgs")


class NotifylistResponse(Response):
    """
    attributes:

    data:
    type    - type
    nid    - notify id
    """

    codestr = "NOTIFYLIST"
    codehead = ()
    codetail = ("type", "nid")
    coderep = ()


class NotifygetMessageResponse(Response):
    """
    attributes:

    data:
    nid    - notify id
    uid    - from user id
    uname    - from username
    date    - date
    type    - type
    title    - title
    body    - body
    """

    codestr = "NOTIFYGET_MESSAGE"
    codehead = ()
    codetail = ("nid", "uid", "uname", "date", "type", "title", "body")
    coderep = ()


class NotifygetNotifyResponse(Response):
    """
    attributes:

    data:
    aid    - aid
    type    - type
    count    - count
    date    - date
    name    - anime name
    """

    codestr = "NOTIFYGET_NOTIFY"
    codehead = ()
    codetail = ("aid", "type", "count", "date", "name")
    coderep = ()


class SendmsgSuccessfulResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "SENDMSG_SUCCESSFUL"
    codehead = ()
    codetail = ()
    coderep = ()


class UserResponse(Response):
    """
    attributes:

    data:
    uid    - user id
    """

    codestr = "USER"
    codehead = ()
    codetail = ("uid",)
    coderep = ()


class PongResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "PONG"
    codehead = ()
    codetail = ()
    coderep = ()


class AuthpongResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "AUTHPONG"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchResourceResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_RESOURCE"
    codehead = ()
    codetail = ()
    coderep = ()


class ApiPasswordNotDefinedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "API_PASSWORD_NOT_DEFINED"
    codehead = ()
    codetail = ()
    coderep = ()


class FileAlreadyInMylistResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "FILE_ALREADY_IN_MYLIST"
    codehead = ()
    codetail = ()
    coderep = ()


class MylistEntryEditedResponse(Response):
    """
    attributes:

    data:
    entries    - number of entries edited
    """

    codestr = "MYLIST_ENTRY_EDITED"
    codehead = ()
    codetail = ("entries",)
    coderep = ()


class MultipleMylistEntriesResponse(Response):
    """
    attributes:

    data:
    name       - anime title
    eps       - episodes
    unknowneps - eps with state unknown
    hddeps       - eps with state on hdd
    cdeps       - eps with state on cd
    deletedeps - eps with state deleted
    watchedeps - watched eps
    gshortname - group short name
    geps       - eps for group
    """

    codestr = "MULTIPLE_MYLIST_ENTRIES"
    codehead = ()
    codetail = ("name", "eps", "unknowneps", "hddeps", "cdeps", "deletedeps", "watchedeps")
    coderep = ("gshortname", "geps")


class SizeHashExistsResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "SIZE_HASH_EXISTS"
    codehead = ()
    codetail = ()
    coderep = ()


class InvalidDataResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "INVALID_DATA"
    codehead = ()
    codetail = ()
    coderep = ()


class StreamnoidUsedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "STREAMNOID_USED"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchFileResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_FILE"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchEntryResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_ENTRY"
    codehead = ()
    codetail = ()
    coderep = ()


class MultipleFilesFoundResponse(Response):
    """
    attributes:

    data:
    fid    - file id
    """

    codestr = "MULTIPLE_FILES_FOUND"
    codehead = ()
    codetail = ()
    coderep = ("fid",)


class NoGroupsFoundResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO GROUPS FOUND"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchAnimeResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_ANIME"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchEpisodeResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_EPISODE"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchProducerResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_PRODUCER"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchGroupResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_GROUP"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyAlreadyAddedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_ALREADY_ADDED"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchBuddyResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_BUDDY"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyAlreadyAcceptedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_ALREADY_ACCEPTED"
    codehead = ()
    codetail = ()
    coderep = ()


class BuddyAlreadyDeniedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BUDDY_ALREADY_DENIED"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchVoteResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_VOTE"
    codehead = ()
    codetail = ()
    coderep = ()


class InvalidVoteTypeResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "INVALID_VOTE_TYPE"
    codehead = ()
    codetail = ()
    coderep = ()


class InvalidVoteValueResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "INVALID_VOTE_VALUE"
    codehead = ()
    codetail = ()
    coderep = ()


class PermvoteNotAllowedResponse(Response):
    """
    attributes:

    data:
    aname    - name of the anime
    """

    codestr = "PERMVOTE_NOT_ALLOWED"
    codehead = ()
    codetail = ("aname",)
    coderep = ()


class AlreadyPermvotedResponse(Response):
    """
    attributes:

    data:
    name    - aname/ename/gname
    """

    codestr = "ALREADY_PERMVOTED"
    codehead = ()
    codetail = ("name",)
    coderep = ()


class NotificationDisabledResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NOTIFICATION_DISABLED"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchPacketPendingResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_PACKET_PENDING"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchEntryMResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_ENTRY_M"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchEntryNResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_ENTRY_N"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchMessageResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_MESSAGE"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchNotifyResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_NOTIFY"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchUserResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_USER"
    codehead = ()
    codetail = ()
    coderep = ()


class NoChanges(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_CHANGES"
    codehead = ()
    codetail = ()
    coderep = ()


class NotLoggedInResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NOT_LOGGED_IN"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchMylistFileResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_MYLIST_FILE"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchMylistEntryResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_MYLIST_ENTRY"
    codehead = ()
    codetail = ()
    coderep = ()


class LoginFailedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "LOGIN_FAILED"
    codehead = ()
    codetail = ()
    coderep = ()


class LoginFirstResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "LOGIN_FIRST"
    codehead = ()
    codetail = ()
    coderep = ()


class AccessDeniedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ACCESS_DENIED"
    codehead = ()
    codetail = ()
    coderep = ()


class ClientVersionOutdatedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "CLIENT_VERSION_OUTDATED"
    codehead = ()
    codetail = ()
    coderep = ()


class ClientBannedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "CLIENT_BANNED"
    codehead = ()
    codetail = ()
    coderep = ()


class IllegalInputOrAccessDeniedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ILLEGAL_INPUT_OR_ACCESS_DENIED"
    codehead = ()
    codetail = ()
    coderep = ()


class InvalidSessionResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "INVALID_SESSION"
    codehead = ()
    codetail = ()
    coderep = ()


class NoSuchEncryptionTypeResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "NO_SUCH_ENCRYPTION_TYPE"
    codehead = ()
    codetail = ()
    coderep = ()


class EncodingNotSupportedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ENCODING_NOT_SUPPORTED"
    codehead = ()
    codetail = ()
    coderep = ()


class BannedResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "BANNED"
    codehead = ()
    codetail = ()
    coderep = ()


class UnknownCommandResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "UNKNOWN_COMMAND"
    codehead = ()
    codetail = ()
    coderep = ()


class InternalServerErrorResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "INTERNAL_SERVER_ERROR"
    codehead = ()
    codetail = ()
    coderep = ()


class AnidbOutOfServiceResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "ANIDB_OUT_OF_SERVICE"
    codehead = ()
    codetail = ()
    coderep = ()


class ServerBusyResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "SERVER_BUSY"
    codehead = ()
    codetail = ()
    coderep = ()


class ApiViolationResponse(Response):
    """
    attributes:

    data:
    """

    codestr = "API_VIOLATION"
    codehead = ()
    codetail = ()
    coderep = ()


class VersionResponse(Response):
    """
    attributes:

    data:
    version    - server version
    """

    codestr = "VERSION"
    codehead = ()
    codetail = ("version",)
    coderep = ()


responses = {
    "200": LoginAcceptedResponse,
    "201": LoginAcceptedNewVerResponse,
    "203": LoggedOutResponse,
    "205": ResourceResponse,
    "206": StatsResponse,
    "207": TopResponse,
    "208": UptimeResponse,
    "209": EncryptionEnabledResponse,
    "210": MylistEntryAddedResponse,
    "211": MylistEntryDeletedResponse,
    "214": AddedFileResponse,
    "215": AddedStreamResponse,
    "219": EncodingChangedResponse,
    "220": FileResponse,
    "221": MylistResponse,
    "222": MylistStatsResponse,
    "225": GroupstatusResponse,
    "230": AnimeResponse,
    "231": AnimeBestMatchResponse,
    "232": RandomanimeResponse,
    "240": EpisodeResponse,
    "245": ProducerResponse,
    "246": NotificationAddedResponse,
    "248": NotificationUpdatedResponse,
    "250": GroupResponse,
    "253": BuddyListResponse,
    "254": BuddyStateResponse,
    "255": BuddyAddedResponse,
    "256": BuddyDeletedResponse,
    "257": BuddyAcceptedResponse,
    "258": BuddyDeniedResponse,
    "260": VotedResponse,
    "261": VoteFoundResponse,
    "262": VoteUpdatedResponse,
    "263": VoteRevokedResponse,
    "270": NotificationEnabledResponse,
    "271": NotificationNotifyResponse,
    "272": NotificationMessageResponse,
    "273": NotificationBuddyResponse,
    "274": NotificationShutdownResponse,
    "280": PushackConfirmedResponse,
    "281": NotifyackSuccessfulMResponse,
    "282": NotifyackSuccessfulNResponse,
    "290": NotificationResponse,
    "291": NotifylistResponse,
    "292": NotifygetMessageResponse,
    "293": NotifygetNotifyResponse,
    "294": SendmsgSuccessfulResponse,
    "295": UserResponse,
    "300": PongResponse,
    "301": AuthpongResponse,
    "305": NoSuchResourceResponse,
    "309": ApiPasswordNotDefinedResponse,
    "310": FileAlreadyInMylistResponse,
    "311": MylistEntryEditedResponse,
    "312": MultipleMylistEntriesResponse,
    "314": SizeHashExistsResponse,
    "315": InvalidDataResponse,
    "316": StreamnoidUsedResponse,
    "320": NoSuchFileResponse,
    "321": NoSuchEntryResponse,
    "322": MultipleFilesFoundResponse,
    "325": NoGroupsFoundResponse,
    "330": NoSuchAnimeResponse,
    "340": NoSuchEpisodeResponse,
    "345": NoSuchProducerResponse,
    "350": NoSuchGroupResponse,
    "355": BuddyAlreadyAddedResponse,
    "356": NoSuchBuddyResponse,
    "357": BuddyAlreadyAcceptedResponse,
    "358": BuddyAlreadyDeniedResponse,
    "360": NoSuchVoteResponse,
    "361": InvalidVoteTypeResponse,
    "362": InvalidVoteValueResponse,
    "363": PermvoteNotAllowedResponse,
    "364": AlreadyPermvotedResponse,
    "370": NotificationDisabledResponse,
    "380": NoSuchPacketPendingResponse,
    "381": NoSuchEntryMResponse,
    "382": NoSuchEntryNResponse,
    "392": NoSuchMessageResponse,
    "393": NoSuchNotifyResponse,
    "394": NoSuchUserResponse,
    "399": NoChanges,
    "403": NotLoggedInResponse,
    "410": NoSuchMylistFileResponse,
    "411": NoSuchMylistEntryResponse,
    "500": LoginFailedResponse,
    "501": LoginFirstResponse,
    "502": AccessDeniedResponse,
    "503": ClientVersionOutdatedResponse,
    "504": ClientBannedResponse,
    "505": IllegalInputOrAccessDeniedResponse,
    "506": InvalidSessionResponse,
    "509": NoSuchEncryptionTypeResponse,
    "519": EncodingNotSupportedResponse,
    "555": BannedResponse,
    "598": UnknownCommandResponse,
    "600": InternalServerErrorResponse,
    "601": AnidbOutOfServiceResponse,
    "602": ServerBusyResponse,
    "666": ApiViolationResponse,
    "998": VersionResponse,
}
