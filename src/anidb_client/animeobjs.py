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
import datetime
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from typing import Any, override

import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm
import sqlalchemy.orm.exc
from sqlalchemy.orm import Session

import anidb_client
import anidb_client.anames
import anidb_client.fileinfo
import anidb_client.mapper
from anidb_client.commands import (
    AnimeCommand,
    Command,
    EpisodeCommand,
    FileCommand,
    GroupCommand,
    MyListAddCommand,
    MyListCommand,
    MyListDelCommand,
)
from anidb_client.db import (
    AnimeRelationTable,
    AnimeTable,
    EpisodeTable,
    FileTable,
    GroupRelationTable,
    GroupTable,
)
from anidb_client.errors import AniDBError, AniDBFileError, IllegalAnimeObject
from anidb_client.link import AniDBLink
from anidb_client.responses import Response

# Ceiling on the fanart.tv rate-limit back-off. Retry-After is chosen by the remote
# server, and an uncapped sleep on the calling thread is exactly the failure SPEC-002
# gives the UDP ban back-off a ceiling to avoid: a client that has, for practical
# purposes, stopped, while reporting only that it is waiting.
FANART_MAX_BACKOFF = 300


def _required[T](value: T | None, what: str) -> T:
    """A value an internal invariant guarantees is present, stated rather than assumed.

    These are branches that only run once the thing they need has been set: an
    Episode built from an anime and an episode number always has the anime, and
    the request it sends cannot be addressed without it. Reading straight through
    was an AttributeError on None, raised from inside a response thread with
    nothing in it naming what was missing.
    """
    if value is None:
        raise IllegalAnimeObject(f"{what} is not available on this object")
    return value


def _relation_rows(paired: Iterable[tuple[str, str]]) -> list[AnimeRelationTable]:
    """Build relation rows from an ANIME reply, skipping pairs that cannot be named.

    Both halves of a pair come from AniDB and both used to be read without a guard:
    `int(aid)` raises ValueError on a non-numeric id, and `anime_relation_map[code]`
    raises KeyError on any relation code the table does not list -- and AniDB extends
    that enumeration without announcing it. This runs inside the ANIME response
    callback, which sets the event the caller is blocked on only as its last
    statement, so either exception is not a dropped field: it escapes the callback,
    the event is never set, and the application waits forever with nothing logged.
    One unrecognised relation code hung the caller permanently.

    An unusable pair is therefore dropped, and said so, which is the tolerance the
    `strict=False` zip at the call site already has for the same "AniDB returned
    something odd" reason. Dropping loses the edge -- a related_anime() walk will not
    follow that link -- and the warning is both the mitigation and the signal that
    `anime_relation_map` has fallen behind. The alternatives were rejected in #5:
    `AnimeRelationTable.relation_type` is a non-nullable Enum over the same
    vocabulary, so storing the edge without a type needs a schema change and this
    project has no migration story, and defaulting to "other" would be
    indistinguishable from the "other" AniDB itself sends as code 100.
    """
    rows = []
    for aid_text, type_code in paired:
        relation_type = anidb_client.mapper.anime_relation_map.get(type_code)
        if relation_type is None:
            anidb_client.log.warning(
                f"Unknown AniDB anime relation code {type_code!r} for related aid {aid_text!r}; skipping that relation"
            )
            continue
        try:
            related_aid = int(aid_text)
        except ValueError:
            anidb_client.log.warning(f"Non-numeric AniDB related aid {aid_text!r}; skipping that relation")
            continue
        rows.append(AnimeRelationTable(related_aid=related_aid, relation_type=relation_type))
    return rows


def _expanded_epno(epno: str) -> list[str]:
    """Expand a ranged episode number: "5-7" becomes ["5", "6", "7"].

    How AniDB records one file covering several episodes, and the one part of a
    file's episode set that is derived from the cache alone. Kept here rather than
    inside `multiep` because the mylist *removal* path needs this much and no more:
    it must expand a range, and it must not consult the filename the way the
    property may (#12).

    Strings, like every other route to an episode set -- containment and both mylist
    loops compare against `Episode.episode_number`, which is text.

    A range whose endpoints are not both numbers is answered as the single episode
    number it came in as, which is what the removal path already did with it. That
    keeps a deletion path from acquiring a new way to raise on data AniDB controls,
    where before it sent the value through untouched.
    """
    if "-" not in epno:
        return [epno]
    start, _, stop = epno.partition("-")
    try:
        return [str(x) for x in range(int(start), int(stop) + 1)]
    except ValueError:
        anidb_client.log.debug(f"Episode number {epno!r} looks like a range but does not expand; taking it as one")
        return [epno]


def _group_relation_rows(entries: Iterable[str]) -> list[GroupRelationTable]:
    """The same rule for a GROUP reply, which words its relations differently.

    A GROUP reply carries one "gid,code" string per relation rather than two
    parallel lists, but the failure was identical -- `group_relation_map[code]` with
    no default, in a callback whose last statement is the one that releases the
    caller -- and so is the answer. See _relation_rows above for the reasoning; the
    group vocabulary is constrained by `GroupRelationTable.relation_type` in exactly
    the same way, and code 6 is already the "other" AniDB sends itself.
    """
    rows = []
    for entry in entries:
        gid_text, separator, type_code = entry.partition(",")
        if not separator:
            # What the comprehension's `if "," in x` filter did, kept as it was.
            continue
        relation_type = anidb_client.mapper.group_relation_map.get(type_code)
        if relation_type is None:
            anidb_client.log.warning(
                f"Unknown AniDB group relation code {type_code!r} for related gid {gid_text!r}; skipping that relation"
            )
            continue
        try:
            related_gid = int(gid_text)
        except ValueError:
            # Not a hang on its own -- a non-numeric gid reaches a non-nullable
            # integer column and fails the commit, which is caught and logged. But
            # that loses the whole group row rather than the one bad relation, so it
            # is dropped by the same rule as the code above.
            anidb_client.log.warning(f"Non-numeric AniDB related gid {gid_text!r}; skipping that relation")
            continue
        rows.append(GroupRelationTable(related_gid=related_gid, relation_type=relation_type))
    return rows


class AniDBObj:
    # The cached row: an AnimeTable, EpisodeTable, FileTable or GroupTable, or None
    # before one has been found. `Any` rather than that union because the policy
    # methods here read fields the four rows share -- `updated`, `last_update_dice`
    # -- without a common base declaring them, and because every attribute a caller
    # reads off one of these objects arrives through __getattr__ below, which cannot
    # be typed more precisely than the row it forwards to.
    db_data: Any

    def __init__(self) -> None:
        self._anidb_link = anidb_client._anidb
        self._illegal_object = False
        self._updated = threading.Event()
        self._updating = threading.Lock()
        # datetime.UTC, not a hand-built zero offset. Same object, and it says
        # what it means.
        self._timezone = datetime.UTC
        self.db_data = None

    def _link(self) -> AniDBLink:
        """The transport, for a path that has decided it needs the network.

        None when the library was initialised with db_only (SPEC-006), where it is
        a cache in front of nothing. Reaching a request from there used to be an
        AttributeError on None raised inside a response thread; this says what
        actually happened.
        """
        if self._anidb_link is None:
            raise AniDBError("This client was initialised with db_only, so it has no AniDB link")
        return self._anidb_link

    def _to_timezoneaware(self, obj: datetime.datetime) -> datetime.datetime:
        if obj.tzinfo is None or obj.tzinfo.utcoffset(obj) is None:
            return obj.replace(tzinfo=self._timezone)
        return obj

    def _fetch_anidb_data(self, block: bool) -> None:
        anidb_client.log.debug(f"Sending anidb request for {self}")
        thread = threading.Thread(target=self._send_anidb_update_req, kwargs={"prio": block})
        thread.start()
        if block:
            thread.join()
            if self._illegal_object:
                raise IllegalAnimeObject(f"{self} is not a valid AniDB object")

    def update(self, block: bool = False) -> None:
        locked = self._updating.acquire(False)
        if not locked:
            if block:
                self._updating.acquire(True)
                self._updating.release()
            return
        self._fetch_anidb_data(block=block)

    def _extra_refresh_probability(self) -> int:
        return 0

    def _probability_of_refresh(self) -> int:
        """Percent chance that this object's cached data should be re-fetched.

        Extracted from update_if_old so the policy can be read and tested on its
        own. It decides how often this library talks to an API that bans clients
        for talking to it too often, which makes it worth being able to state
        exactly, rather than inferring it from whether a request happened to go
        out.

        Nothing for the first week, 2% in the second, then about half again each
        week after -- plus whatever the subclass adds -- capped at 100.
        """
        age = datetime.datetime.now(self._timezone) - self._to_timezoneaware(self.db_data.updated)
        # Whole weeks since the data was fetched. The original counted this by
        # subtracting a week from `age` each pass and breaking when it went
        # negative, which worked but hid the loop's actual bound in a mutating
        # local. Same number of terms, stated directly.
        weeks_old = age // datetime.timedelta(weeks=1)

        class_probability = self._extra_refresh_probability()
        refresh_probability = 0
        for _week in range(weeks_old):
            if refresh_probability >= 100:
                break
            refresh_probability = 2 if not refresh_probability else math.ceil(refresh_probability * 1.5)

        total = min(100, refresh_probability + class_probability)
        anidb_client.log.debug(f"Probability of updating {self}: {total}% ({class_probability}% from class rules)")
        return total

    def update_if_old(self, block: bool = False) -> None:
        if not self.db_data:
            self.update(block=True)
        else:
            age = datetime.datetime.now(self._timezone) - self._to_timezoneaware(self.db_data.updated)
            # never update twice the same day...
            if age < datetime.timedelta(days=1):
                return
            # also, if we've already calculated the update probability recently
            # we should not re-cacluclate it. Timeout is 20 hours which should
            # be enough for not triggering often, but still allow a daily
            # cronjob to update the cache every day.
            time_since_dice = datetime.datetime.now(self._timezone) - self._to_timezoneaware(
                self.db_data.last_update_dice
            )
            if time_since_dice < datetime.timedelta(hours=20):
                return

            refresh_probability = self._probability_of_refresh()

            with self._db_session() as sess:
                self.db_data = sess.merge(self.db_data)
                self.db_data.last_update_dice = datetime.datetime.now(self._timezone)
                self._db_commit(sess)

            # randint is inclusive at both ends, so the old randint(0, 100) drew from
            # 101 values and `<= probability` fired for probability + 1 of them. A
            # 0% probability -- what _probability_of_refresh returns for data that is
            # not due -- still spent a rate-limited UDP call about 1% of the time.
            if random.randint(1, 100) <= refresh_probability:
                self.update(block=block)

    def _send_anidb_update_req(self, prio: bool = False) -> None:
        # NotImplementedError, not a bare Exception: `except Exception` around a
        # call site would swallow a missing override as though it were a runtime
        # failure. Every subclass overrides this; the signature matches theirs.
        raise NotImplementedError

    def _close_db_session(self, session: Session) -> None:
        session.close()

    def _get_db_session(self) -> Session:
        return anidb_client.get_session()

    @contextlib.contextmanager
    def _db_session(self) -> Iterator[Session]:
        """Open a cache session for the duration of a block, and always close it.

        Every use of a session in this module used to be two statements, an open
        and a close, with the body between them -- and anything raising in that
        body skipped the close and leaked the pooled connection. The surrounding
        `except` clauses log a database error rather than propagating it (SPEC-003
        makes cache writes best-effort), so the leak was silent, and it was worst
        exactly where the code takes an early `return` out of the middle.

        This adds only the open/close pairing. It does not commit, and it does not
        swallow anything: the callers that must not propagate a database error
        keep the `except` clause they already had, so what commits and what is
        logged is unchanged. SQLAlchemy's own `Session.begin()` would commit and
        re-raise, which is the opposite of the best-effort rule.
        """
        session = self._get_db_session()
        try:
            yield session
        finally:
            self._close_db_session(session)

    def _db_commit(self, session: Session) -> None:
        try:
            session.commit()
            anidb_client.log.debug(f"Object saved to database: {self.db_data}")
        except sqlalchemy.exc.DBAPIError as e:
            if self.db_data:
                anidb_client.log.warning(f"Failed to update data {self.db_data}: {e}")
            else:
                anidb_client.log.warning(f"Failed to update db: {e}")
            session.rollback()

    def __getattribute__(self, attr: str) -> Any:
        # Internal machinery stays readable on an object that has been marked
        # illegal. Everything here is either the flag itself or a completion event
        # that a waiting thread depends on -- File's `_file_updated` and
        # `_mylist_updated` were missing, so a callback that marked the file illegal
        # then raised on its own `self._file_updated.set()`, leaving the waiter
        # blocked. Marking an object invalid must never make it unable to say so.
        if attr in ["_updated", "_updating", "_anidb_link", "_file_updated", "_mylist_updated", "_illegal_object"]:
            return super().__getattribute__(attr)
        if super().__getattribute__("_illegal_object"):
            raise IllegalAnimeObject(f"{self} is not a valid AniDB object")
        return super().__getattribute__(attr)

    def __getattr__(self, name: str) -> Any:
        # `Any` is the honest answer and the boundary of what this module can be
        # checked at: this forwards to whichever column of whichever cached row
        # carries `name`, answering None for one that does not exist. A caller
        # reading `anime.rating` reaches a float through here and `anime.picname` a
        # string, and nothing static can say which without the row in hand.
        local_vars = vars(self)
        if name not in ("updated", "relations"):
            local_name = f"_{name}"
            # `is not None` rather than a truth test. A cached value that is
            # legitimately falsy -- 0 votes, an empty description, False -- read as
            # "not fetched yet" and fell through to update_if_old(), which can spend
            # a rate-limited UDP call to be told the same zero again.
            if local_name in local_vars and local_vars[local_name] is not None:
                return local_vars[local_name]

        super().__getattribute__("_updating").acquire()
        super().__getattribute__("_updating").release()
        super().__getattribute__("update_if_old")()
        if name == "relations":
            # Read the relationship off db_data. This was `self.relations`, which for
            # any class without a `relations` property -- Group, Episode, File --
            # re-entered __getattr__ on the same name and recursed until the stack
            # ran out. Anime was unaffected only because it declares the property, so
            # this branch was never reached for it.
            db_data = super().__getattribute__("db_data")
            # Asked of the class, so that a table which simply has no relations
            # answers None -- as the fall-through below does for any other unknown
            # attribute -- without touching the instance and triggering a load.
            if db_data is None or not hasattr(type(db_data), "relations"):
                return None
            try:
                relations = db_data.relations
            except sqlalchemy.orm.exc.DetachedInstanceError:
                # _get_db_data() closes the session it used, so the cached row is
                # detached and a lazy relationship cannot load through it. Re-attach
                # a copy for the read. Anime's own `relations` property recovers the
                # same way; this is that recovery for every other class, which
                # previously could not get this far to need it.
                with self._db_session() as sess:
                    relations = list(sess.merge(db_data).relations)
            if relations is None or isinstance(relations, list):
                return relations
            return relations()
        return getattr(super().__getattribute__("db_data"), name, None)


class Anime(AniDBObj):
    def __init__(self, init: int | str) -> None:
        super().__init__()
        self._aid: int | None = None
        self._titles: list[AnimeTitle] | None = None
        self._title: str | None = None
        self._in_mylist: bool | None = None

        try:
            if isinstance(init, int):
                self._aid, self._titles, score, best_title = anidb_client.anames.get_titles(aid=init)[0]
            elif isinstance(init, str):
                self._aid, self._titles, score, best_title = anidb_client.anames.get_titles(name=init)[0]
        except IndexError:
            raise IllegalAnimeObject(f"No title match for '{init}'") from None

        self._title = [x.title for x in self.titles if x.lang is None and x.titletype == "main"][0]
        self.db_data = None
        self._get_db_data()

    @override
    def _extra_refresh_probability(self) -> int:
        ref = datetime.timedelta()
        # The shorter time there is between when anidb updated this
        # anime and we fetched our data, the more likely is it that it has
        # changed again. So we start at 30%, and removes 10% for each week
        probability = 30
        data_age = self._to_timezoneaware(self.db_data.updated) - self.db_data.anidb_updated.replace(
            tzinfo=self._timezone
        )
        while probability > 0:
            data_age -= datetime.timedelta(weeks=1)
            if data_age < ref:
                break
            probability -= 10
        return max(probability, 0)

    def _get_db_data(self) -> None:
        with self._db_session() as sess:
            res = sess.query(AnimeTable).filter_by(aid=self.aid).all()
            if len(res) > 0:
                self.db_data = res[0]

    def _db_data_callback(self, res: Response) -> None:
        # "NO SUCH ANIME" carries no data lines, so this check has to come before
        # anything reads them. It used to sit *after* `res.datalines[0]`, which
        # raised IndexError on every 330 reply -- and because the exception left
        # `_updated` unset, whoever was waiting on it waited forever. Asking for an
        # aid AniDB does not have hung the caller permanently.
        if res.rescode == "330":
            # `self.log` -- there is no such attribute on AniDBObj, and by this point
            # `_illegal_object` had already been set, so __getattribute__ raised
            # IllegalAnimeObject before `_updated.set()` could run. That left the 330
            # path hanging even once the IndexError above it was fixed. Log through
            # the module, and set the flag after, so neither can bite again.
            anidb_client.log.warning(f"{self} is not a valid Anime object")
            self._illegal_object = True
            self._updated.set()
            return

        # Separate from `relations` below, which used to be this and then the rows
        # built from it -- two types under one name, and unbindable when the reply
        # carried no relations at all.
        paired: Iterable[tuple[str, str]] = []
        new = None
        ainfo: dict[str, Any] = res.datalines[0]

        if all(x in ainfo and ainfo[x] for x in ["related_aid_list", "related_aid_type"]):
            # strict=False: AniDB has been seen to return the two lists at different
            # lengths, and the pre-existing behaviour is to pair what it can.
            paired = zip(ainfo["related_aid_list"].split("'"), ainfo["related_aid_type"].split("'"), strict=False)
        if "related_aid_list" in ainfo:
            del ainfo["related_aid_list"]
        if "related_aid_type" in ainfo:
            del ainfo["related_aid_type"]
        relations = _relation_rows(paired)

        # convert datatypes
        for attr, data in ainfo.items():
            if attr in anidb_client.mapper.anime_map_a_converters:
                ainfo[attr] = anidb_client.mapper.anime_map_a_converters[attr](data)

        try:
            with self._db_session() as sess:
                if self.db_data:
                    self.db_data = sess.merge(self.db_data)
                    self.db_data.update(**ainfo)
                    self.db_data.updated = datetime.datetime.now(self._timezone)
                    new_relations = []
                    for r in relations:
                        found = False
                        for sr in self.db_data.relations:
                            if r.related_aid == sr.related_aid:
                                found = True
                                sr.relation_type = r.relation_type
                                sr.anime_pk = self.db_data.pk
                                new_relations.append(sr)
                        if not found:
                            r.anime_pk = self.db_data.pk
                            new_relations.append(r)
                    for r in self.db_data.relations:
                        if r not in new_relations:
                            sess.delete(r)
                    self.db_data.relations = new_relations
                else:
                    new = AnimeTable(**ainfo)
                    new.updated = datetime.datetime.now(self._timezone)
                    new.last_update_dice = datetime.datetime.now(self._timezone)
                    new.relations = relations
                    # commit to sql database
                    sess.add(new)

                if new:
                    self.db_data = new
                self._db_commit(sess)
        except sqlalchemy.exc.OperationalError:
            anidb_client.log.error(f"Failed to update {self} in database")
        self._updated.set()

    @override
    def _send_anidb_update_req(self, prio: bool = False) -> None:
        self._updated.clear()
        req = AnimeCommand(aid=str(self.aid), amask=anidb_client.mapper.getAnimeBitsA(anidb_client.mapper.anime_map_a))
        self._link().request(req, self._db_data_callback, prio=prio)
        self._updated.wait()
        self._updating.release()

    @property
    def in_mylist(self) -> bool | None:
        if self._in_mylist is not None:
            return self._in_mylist
        try:
            with self._db_session() as sess:
                res = sess.query(FileTable).filter(FileTable.aid == self._aid, FileTable.lid.is_not(None)).first()
            self._in_mylist = bool(res)
        except sqlalchemy.exc.OperationalError as e:
            anidb_client.log.error(f"Failed to get mylist status of {self} from database: {e}")
            return None
        return self._in_mylist

    @property
    def relations(self) -> list[tuple[str, Anime]]:
        try:
            relations = [(x.relation_type, Anime(x.related_aid)) for x in self.db_data.relations]
        except sqlalchemy.orm.exc.DetachedInstanceError:
            # The cached row is detached, so its lazy relationship cannot load
            # through it. Re-attach a copy for the read, which is what __getattr__
            # does for every class that has no `relations` property of its own.
            #
            # This used to be `_get_db_data(close=False)` -- a re-query on a
            # session deliberately left open so the row stayed attached, followed
            # by a second, unused session that was the only one closed. That is the
            # one site here where the connection leaked on the ordinary path rather
            # than only on an error, and `close` had no other caller.
            with self._db_session() as sess:
                self.db_data = sess.merge(self.db_data)
                relations = [(x.relation_type, Anime(x.related_aid)) for x in self.db_data.relations]
        return relations

    def related_anime(self, exclude: Iterable[Anime] | None = None, only_in_mylist: bool = True) -> list[Anime]:
        """Walk this anime's relations transitively and return the connected set.

        The returned list starts with this Anime, followed by every Anime
        reachable by following relation links. `exclude` is an iterable of Anime
        treated as walls: neither returned nor traversed through. While
        `only_in_mylist` is set the walk follows only anime that are in mylist,
        which stops a single sequel link from dragging in an entire franchise.
        """
        excluded = list(exclude) if exclude else []
        found: list[Anime] = [self]
        queue: list[Anime] = []

        def _followable(anime: Anime) -> bool:
            # Anime defines __eq__ but not __hash__, so it is unhashable and
            # membership here is list scans rather than set lookups. Relation
            # neighbourhoods are small enough that this does not matter.
            if anime in excluded or anime in found or anime in queue:
                return False
            return not only_in_mylist or bool(anime.in_mylist)

        try:
            queue.extend(a for _relation_type, a in self.relations if _followable(a))
            # `queue` is appended to while it is being iterated: that is the
            # traversal, not an accident. Anime relations form a cyclic graph
            # (every sequel link has a matching prequel link back), so the
            # _followable membership checks above are what terminate the walk.
            for anime in queue:
                found.append(anime)
                queue.extend(a for _relation_type, a in anime.relations if _followable(a))
        except IllegalAnimeObject as e:
            anidb_client.log.warning(f"Stopped walking relations for {self} after {len(found)} anime: {e}")

        return found

    def extid(self, source: str, id_type: str = "tv") -> str | list[str] | None:
        if source == "thetvdb":
            return anidb_client.anames.get_tvdbid(self.aid, id_type)
        elif source == "tmdb":
            return anidb_client.anames.get_tmdbid(self.aid, id_type)
        elif source == "imdb":
            return anidb_client.anames.get_imdbid(self.aid, id_type)
        # An unrecognised source is an absence, matching every other id lookup here.
        return None

    @property
    def tvdbid(self) -> str | None:
        return anidb_client.anames.get_tvdbid(self.aid)

    @property
    def tmdbid(self) -> str | list[str] | None:
        return anidb_client.anames.get_tmdbid(self.aid)

    @property
    def imdbid(self) -> str | list[str] | None:
        return anidb_client.anames.get_imdbid(self.aid)

    @property
    def fanart(self) -> list[Any]:
        if not anidb_client.fanart_key:
            return []
        ret: list[Any] = []
        headers = {"api-key": anidb_client.fanart_key, "content-type": "application/json"}
        base_url = "https://webservice.fanart.tv/"

        # Currently fanart.tv only supports tvdbids for tv fanart
        tv_id = self.extid("thetvdb", "tv")
        movie_ids = [x for x in [self.extid("tmdb", "movie"), self.extid("imdb", "movie")] if x]
        urls: list[str] = []
        for i in movie_ids:
            if type(i) is str:
                urls.append(urllib.parse.urljoin(base_url, f"/v3.2/movies/{i}"))
            elif type(i) is list:
                urls.extend([urllib.parse.urljoin(base_url, f"/v3.2/movies/{x}") for x in i])
        if tv_id:
            urls.append(urllib.parse.urljoin(base_url, f"/v3.2/tv/{tv_id}"))

        for url in urls:
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=anidb_client.HTTP_TIMEOUT) as f:
                    res = json.load(f)
            except urllib.error.HTTPError as e:
                # 429 is tested before the catch-all. It used to be tested after
                # `if e.code != 404: return []`, which 429 satisfies -- so the
                # back-off below could never run and a rate-limited reply was
                # treated as a hard failure.
                if e.code == 429:
                    try:
                        asked = int(e.headers.get("Retry-After", 0))
                    except TypeError, ValueError:
                        # Retry-After is also allowed to be an HTTP-date. Not worth
                        # parsing for this: move on rather than guess at a delay.
                        asked = 0
                    retry_after = min(asked, FANART_MAX_BACKOFF)
                    if retry_after < asked:
                        anidb_client.log.warning(f"Fanart asked for {asked}s; waiting {retry_after}s and moving on")
                    else:
                        anidb_client.log.warning(f"Fanart ratelimited, sleeping for {retry_after} seconds")
                    time.sleep(retry_after)
                elif e.code != 404:
                    anidb_client.log.error(f"Failed to fetch fanart at {url}: {e}")
                    return ret
                # 404 and 429 both leave this url without a result and move on to
                # the next one, rather than failing the whole lookup.
                res = None
            except urllib.error.URLError as e:
                # `ret`, not []. An anime can be mapped at several sources, and
                # artwork already gathered from one is not made worthless by a later
                # one failing -- returning [] discarded it silently.
                anidb_client.log.warning(f"Failed to fetch fanart at {url}: {e}")
                return ret
            if res:
                ret.append(res)

        return ret

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Anime):
            return NotImplemented
        return bool(self.aid == other.aid)

    def __contains__(self, other: object) -> bool:
        # `False`, not `NotImplemented`. There is no reflected form of __contains__
        # for Python to fall back to -- `in` coerces whatever this returns to a bool
        # -- and since 3.14 that coercion raises TypeError instead of warning. So
        # returning NotImplemented made `anything in anime` raise for every operand
        # that was not an Episode, where the intent was plainly "no, it is not in
        # there". __eq__ below is the case where NotImplemented is right, because
        # Python really does fall back to identity comparison for it.
        if not isinstance(other, Episode):
            return False
        return bool(other.aid == self.aid)

    def __repr__(self) -> str:
        return "Anime(title='{}', aid={})".format(
            object.__getattribute__(self, "_title"), object.__getattribute__(self, "_aid")
        )


class AnimeTitle:
    # Annotated ahead of the rest of this module: anames.py builds these and is
    # already on the mypy strict list, where an untyped constructor is an error.
    def __init__(self, titletype: str | None, lang: str | None, title: str | None) -> None:
        self.titletype = titletype
        self.lang = lang
        self.title = title

    def __repr__(self) -> str:
        return f"AnimeTitle(titletype='{self.titletype}', lang='{self.lang}', title='{self.title}')"


class Episode(AniDBObj):
    _eid: int | None = None
    _anime: Anime | None = None
    _episode_number: str | None = None
    _in_mylist: bool | None = None
    _part: int | None = None

    @property
    def episode_number(self) -> str:
        if self._episode_number:
            return self._episode_number
        # Declared str rather than optional, and returned as it is rather than
        # through str(): an Episode either carries the number it was built with or
        # resolves one from its cached row, and an eid AniDB does not recognise
        # makes the object illegal before this can be read. str() around it would
        # turn that never-case into the string "None", which is worse than None.
        epno: str = self.epno
        return epno

    @property
    def tvdb_episode(self) -> tuple[int | None, Any]:
        return self._get_ext_epid("tvdb")

    @property
    def tmdb_episode(self) -> tuple[int | None, Any]:
        return self._get_ext_epid("tmdb")

    def _get_ext_epid(self, source: str) -> tuple[int | None, Any]:
        res: tuple[int | None, Any] = anidb_client.anames.get_tv_episode(self.anime.aid, self.episode_number, source)
        # special case if when anidb adds parts as regular episodes on movies
        if self.anime.nr_of_episodes == 1 and self._part is None:
            season, ep = res
            try:
                my_ep = int(self.episode_number)
            except ValueError:
                return res
            if type(ep) is tuple:
                epno, _part = ep
                res = (season, epno) if my_ep == 1 else (season, (epno, my_ep - 1))
        return res

    def _get_mdbid(self, ids: str | list[str] | None) -> str | None:
        if not ids:
            return None
        mdbid = None
        anime = self.anime
        if type(ids) is str:
            ids = [ids]
        if len(ids) == anime.nr_of_episodes:
            # Sometimes anidb adds parts of a movie as episodes > 1, so
            # episode_number can be > 1 even if nr_of_episodes == 1.
            # We're only interested in the first ID in that case.
            if anime.nr_of_episodes == 1:
                return ids[0]
            try:
                mdbid = ids[int(self.episode_number) - 1]
                if not int(mdbid.strip("t")):
                    return None
            except ValueError:
                return None
        return mdbid

    @property
    def tmdbid(self) -> str | None:
        return self._get_mdbid(self.anime.extid("tmdb", "movie"))

    @property
    def imdbid(self) -> str | None:
        return self._get_mdbid(self.anime.extid("imdb", "movie"))

    @property
    def in_mylist(self) -> bool | None:
        if self._in_mylist is not None:
            return self._in_mylist
        try:
            with self._db_session() as sess:
                res = sess.query(FileTable).filter(FileTable.eid == self.eid, FileTable.lid.is_not(None)).first()
            self._in_mylist = bool(res)
        except sqlalchemy.exc.OperationalError as e:
            anidb_client.log.error(f"Failed to get mylist status of {self} from database: {e}")
            return None
        return self._in_mylist

    @property
    def eid(self) -> int | None:
        eid: int | None = self.__getattr__("eid")
        if eid:
            return eid
        elif self.db_data and not self.db_data.eid:
            self.update(True)
        result: int | None = self.db_data.eid
        return result

    def __init__(
        self, anime: Anime | int | str | None = None, epno: str | int | None = None, eid: int | None = None
    ) -> None:
        super().__init__()

        if not ((anime and epno) or eid):
            raise IllegalAnimeObject("Episode must be created with either anime and epno, or eid.")
        if eid:
            self._eid = eid
        if anime:
            if isinstance(anime, Anime):
                self._anime = anime
            else:
                self._anime = Anime(anime)
        if epno:
            epno_text = str(epno)
            with contextlib.suppress(ValueError):
                epno_text = str(int(epno))
            self._episode_number = epno_text
        self.db_data = None
        self._get_db_data()

    def _get_db_data(self) -> None:
        with self._db_session() as sess:
            if self._eid:
                res = sess.query(EpisodeTable).filter_by(eid=self._eid).all()
            else:
                res = (
                    sess.query(EpisodeTable)
                    .filter(
                        EpisodeTable.aid == _required(self._anime, "anime").aid,
                        EpisodeTable.epno.ilike(self.episode_number),
                    )
                    .all()
                )
            if len(res) > 0:
                self.db_data = res[0]
                anidb_client.log.debug(f"Found db_data for episode: {self.db_data}")
                if self.db_data.epno:
                    self._episode_number = self.db_data.epno
                if not self._anime:
                    # An Anime, not the bare aid. Every other path here assigns an
                    # object, and `Episode.anime` is documented as one -- but an
                    # Episode built from an eid took this branch and got an int. The
                    # damage was silent: `self.anime.aid` in _get_ext_epid raised
                    # AttributeError, which Python turns into a __getattr__ lookup,
                    # which returns None for an unknown field. So tvdb_episode and
                    # tmdb_episode simply answered None for every eid-built Episode
                    # rather than failing.
                    self._anime = Anime(self.db_data.aid)

    def _anidb_data_callback(self, res: Response) -> None:
        try:
            with self._db_session() as sess:
                if res.rescode == "340":
                    anidb_client.log.warning(f"No such episode in anidb: {self}")
                    self._illegal_object = True
                    self._updated.set()
                    return
                einfo: dict[str, Any] = res.datalines[0]
                new = None
                for attr, data in einfo.items():
                    if attr == "epno":
                        with contextlib.suppress(ValueError):
                            einfo[attr] = str(int(data))
                        continue
                    if attr in ("title_eng", "title_romaji", "title_kanji"):
                        continue
                    einfo[attr] = anidb_client.mapper.episode_map_converters[attr](data)

                if self.db_data:
                    self.db_data = sess.merge(self.db_data)
                    self.db_data.update(**einfo)
                    self.db_data.updated = datetime.datetime.now(self._timezone)
                else:
                    new = EpisodeTable(**einfo)
                    new.updated = datetime.datetime.now(self._timezone)
                    new.last_update_dice = datetime.datetime.now(self._timezone)
                    sess.add(new)

                if new:
                    self.db_data = new

                self._db_commit(sess)
        except sqlalchemy.exc.OperationalError:
            anidb_client.log.error(f"Failed to update {self} in database")
        self._updated.set()

    @override
    def _send_anidb_update_req(self, prio: bool = False) -> None:
        self._updated.clear()
        if self._eid:
            req = EpisodeCommand(eid=self._eid)
        else:
            req = EpisodeCommand(aid=_required(self._anime, "anime").aid, epno=self.episode_number)
        self._link().request(req, self._anidb_data_callback, prio=prio)
        self._updated.wait()
        self._updating.release()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Episode):
            return NotImplemented
        if self._eid and other._eid:
            return self._eid == other._eid
        if self._lid and other._lid:
            return bool(self._lid == other._lid)
        if self._episode_number and other._episode_number:
            return self._episode_number == other._episode_number
        # `self.eid` and `self.lid` come through __getattr__, so they are Any.
        return bool(self.eid == other.eid or self.lid == other.lid)

    def __repr__(self) -> str:
        return "Episode(anime={}, episode_number='{}', eid={})".format(
            object.__getattribute__(self, "_anime"),
            object.__getattribute__(self, "_episode_number"),
            object.__getattribute__(self, "_eid"),
        )


class File(AniDBObj):
    _anime: Anime | None = None
    _episode: Episode | None = None
    _group: Group | None = None
    # None, not []. A mutable default on the class is shared by every File in the
    # process, so the first `.append` to it would leak one file's episode list into
    # all the others. Nothing appends today -- every write here rebinds -- which is
    # why this has not bitten yet, and is exactly the state in which it is cheap to
    # fix.
    _multiep: list[str] | None = None
    _fid: int | None = None
    _path: str | None = None
    _size: int | None = None
    _ed2khash: str | None = None
    _mtime: datetime.datetime | None = None
    _lid: int | None = None
    _part: int | None = None
    _is_generic: bool | None = None

    @property
    def anime(self) -> Anime:
        if self._anime:
            return self._anime
        self._anime = Anime(self.aid)
        return self._anime

    @property
    def episode(self) -> Episode:
        if self._episode:
            return self._episode
        kwargs: dict[str, Any] = {}
        if self._multiep:
            kwargs["epno"] = self._multiep[0]
        if self._anime:
            kwargs["anime"] = self._anime
        if self.db_data and self.db_data.eid:
            kwargs["eid"] = self.db_data.eid
        if ("epno" in kwargs and "anime" in kwargs) or "eid" in kwargs:
            anidb_client.log.debug(f"Creating episode with {kwargs}")
            self._episode = Episode(**kwargs)
        elif self.eid:
            self._episode = Episode(eid=self.eid)
        else:
            _anime, episodes = self._guess_anime_ep_from_file(aid=_required(self._anime, "anime").aid)
            self._episode = _required(episodes, "episodes")[0]
        return self._episode

    @property
    def in_mylist(self) -> bool:
        return bool(self.lid)

    @property
    def group(self) -> Group | None:
        if self._group:
            return self._group
        if self.gid:
            self._group = Group(gid=self.gid)
            return self._group
        return None

    @property
    def part(self) -> int | None:
        if self._part:
            return self._part
        if self._path:
            f = os.path.basename(self._path)
            m = re.search(anidb_client.fileinfo.partfile_re, f)
            if m:
                try:
                    self._part = int(m.group(2))
                except ValueError:
                    self._part = anidb_client.mapper.roman_numbering[m.group(2)]
                return self._part
        return None

    @property
    def multiep(self) -> list[str]:
        """Return all episode numbers if there are more of them. Note that this
        is very much not reliable since this attribute is not stored in the
        database.

        FIXME: add multiep attribute to database..."""
        if self._multiep:
            return self._multiep

        if "-" in self.episode.episode_number:
            # Through _expanded_epno, which is the same expansion the removal path
            # needs on its own -- it wants a range expanded but not the filename
            # branch below. Sharing it is what keeps the two from drifting apart
            # again, which is the shape of the bug in #12.
            self._multiep = _expanded_epno(self.episode.episode_number)
            return self._multiep

        if self.path:
            episodes = self._guess_epno_from_filename(os.path.split(self.path)[1], self.anime)
            # if database says an episode that is not in episodes list assume
            # name is wrong.
            if episodes:
                epnos = [ep.episode_number for ep in episodes]
                if self.episode.episode_number in epnos:
                    self._multiep = epnos
                    return self._multiep
        self._multiep = [self.episode.episode_number]

        return self._multiep

    @property
    def size(self) -> int | None:
        if self._size:
            return self._size
        if self.path:
            self._mtime, self._size = anidb_client.fileinfo.get_file_stats(self.path, self.nfs_obj)
        elif self.db_data and self.db_data.size:
            self._size = self.db_data.size
        return self._size

    @property
    def mtime(self) -> datetime.datetime | None:
        if self._mtime:
            return self._mtime
        if self.path:
            self._mtime, self._size = anidb_client.fileinfo.get_file_stats(self.path, self.nfs_obj)
        elif self.db_data and self.db_data.mtime:
            self._mtime = self.db_data.mtime
        return self._mtime

    @property
    def ed2khash(self) -> str | None:
        if self._ed2khash:
            return self._ed2khash
        elif self._path:
            if self.db_data and self.db_data.mtime and self.db_data.size and self.db_data.ed2khash:
                mtime, size = anidb_client.fileinfo.get_file_stats(self.path, self.nfs_obj)
                if mtime == self.db_data.mtime and size == self.db_data.size:
                    self._ed2khash = self.db_data.ed2khash

            if self._ed2khash:
                return self._ed2khash

            self._ed2khash = anidb_client.fileinfo.get_file_hash(self._path, self.nfs_obj)
            anidb_client.log.debug(f"Calculated ed2khash: {self._ed2khash}")
            return self._ed2khash

        anidb_client.log.debug("Trying to fetch ed2khash from anidb")
        # wait for any update process to finish
        self._updating.acquire()
        self._updating.release()
        if not self.db_data and not self.db_data.ed2khash:
            self.update_if_old(block=True)
        if self.db_data:
            self._ed2khash = self.db_data.ed2khash
        return self._ed2khash

    def __init__(
        self,
        path: str | None = None,
        fid: int | None = None,
        lid: int | None = None,
        anime: Anime | int | str | None = None,
        episode: Episode | str | None = None,
        nfs_obj: Any = None,
        force_single_episode_series: bool = False,
        parse_dir: bool = True,
    ) -> None:
        super().__init__()
        self.force_single_episode_series = force_single_episode_series
        self.parse_dir = parse_dir
        self._file_updated = threading.Event()
        self._mylist_updated = threading.Event()
        anidb_client.log.debug(f"path: {path}, fid: {fid}, anime: {anime}, episode: {episode}, lid: {lid}")
        if not path and not fid and not (anime and episode) and not lid:
            raise AniDBError("File must be created with either filename, fid, lid or anime and episode.")

        self.nfs_obj = nfs_obj
        if path:
            self._path = path
            self._mtime, self._size = anidb_client.fileinfo.get_file_stats(self._path, self.nfs_obj)
            anidb_client.log.debug(f"Created File {self._path} - size: {self._size}, mtime: {self._mtime}")
        if fid:
            self._fid = int(fid)
        if lid:
            self._lid = int(lid)
        if anime:
            if isinstance(anime, Anime):
                self._anime = anime
            else:
                self._anime = Anime(anime)
        if episode:
            if isinstance(episode, Episode):
                self._episode = episode
            else:
                self._episode = Episode(anime=self._anime, epno=episode)
                self._multiep = [episode]
        self._get_db_data()

    def _get_db_data(self) -> None:
        with self._db_session() as sess:
            res = None
            if self._fid:
                res = sess.query(FileTable).filter_by(fid=self._fid).all()
            elif self._lid:
                res = sess.query(FileTable).filter_by(lid=self._lid).all()
            elif self._path:
                res = sess.query(FileTable).filter_by(path=self._path).all()
                if res and res[0].size != self._size:
                    sess.delete(res[0])
                    self._db_commit(sess)
                    res = []
                if not res:
                    res = sess.query(FileTable).filter_by(size=self._size, ed2khash=self.ed2khash).all()
            elif _required(self._episode, "episode").eid:
                res = (
                    sess.query(FileTable)
                    .filter_by(aid=_required(self._anime, "anime").aid, eid=_required(self._episode, "episode").eid)
                    .all()
                )
                if res and len(res) > 0:
                    res = [x for x in res if x.lid]
            if res and len(res) > 0:
                self.db_data = res[0]
                if self._path and self._path != self.db_data.path:
                    self.db_data.path = self._path
                if not self.db_data.aid or not self.db_data.eid:
                    anime, episodes = self._guess_anime_ep_from_file()
                    self.db_data.aid = _required(anime, "anime").aid
                    self.db_data.eid = _required(episodes, "episodes")[0].eid

                sess.merge(self.db_data)
                self._db_commit(sess)
                anidb_client.log.debug(f"Found db_data for file: {self.db_data}")
                self._is_generic = self.db_data.is_generic
                self._part = self.db_data.part
            if not self._anime and self.db_data and self.db_data.aid:
                self._anime = Anime(self.db_data.aid)

    def _anidb_file_data_callback(self, res: Response) -> None:
        new = None
        update_mylist = False
        finfo: dict[str, Any] = {}
        anidb_client.log.debug(f"Response from anidb about file {self}")
        if res.rescode in ("340", "320"):
            anidb_client.log.debug(f"{self} is not present in AniDB")
            if not self.db_data:
                self._is_generic = True
                if self._anime:
                    anime, episodes = self._guess_anime_ep_from_file(aid=_required(self._anime, "anime").aid)
                else:
                    anime, episodes = self._guess_anime_ep_from_file()
                if anime and episodes:
                    self._multiep = [e.episode_number for e in episodes]
                    self._anime = anime
                    self._episode = episodes[0]
                try:
                    # `anime` is None when there was nothing to guess from -- an fid
                    # AniDB does not have, with no local path. That raised
                    # AttributeError, which this clause did not catch, so the
                    # exception escaped and left the waiter blocked.
                    if anime is None:
                        raise IllegalAnimeObject(f"Could not identify {self}")
                    finfo["aid"] = anime.aid
                    finfo["eid"] = _required(episodes, "episodes")[0].eid
                    finfo["is_generic"] = self._is_generic
                except IllegalAnimeObject, IndexError, TypeError:
                    self._illegal_object = True
                    # Signal before returning. `_file_updated` is otherwise only set
                    # at the very end of this method, so bailing out here left every
                    # waiter blocked forever -- a file AniDB does not know, whose
                    # anime and episode cannot be guessed, hung the caller.
                    self._file_updated.set()
                    return
        else:
            # Copied rather than aliased: `del finfo["state"]` below would
            # otherwise reach back into the response object's own data line.
            finfo.update(res.datalines[0])
            state: int | None = None
            anidb_client.log.debug(f"{self} is in anidb")

            # if this file previously was generic, the file has probably been
            # added to anidb. We should remove any generic file from mylist and
            # add this instead.
            if self.db_data and self.db_data.is_generic:
                update_mylist = True
            else:
                self._is_generic = False

            finfo["is_generic"] = False
            if "state" in finfo:
                state = int(finfo["state"])
                del finfo["state"]

            anidb_client.log.debug("adding attrs to object")
            for attr, data in finfo.items():
                if attr in anidb_client.mapper.file_map_f_converters:
                    finfo[attr] = anidb_client.mapper.file_map_f_converters[attr](data)
                else:
                    finfo[attr] = data

            # Only decode the state bitmask when AniDB actually sent one. `state`
            # stays None when the field is absent from the reply, and `None & 0x1`
            # raises TypeError -- here, on the listener's response thread, before
            # `_file_updated.set()` at the end of this method. The reply had arrived
            # and been parsed, and every waiter on it blocked forever regardless.
            # Absent state means unknown, so the fields it would set stay unset
            # rather than being given invented defaults.
            if state is not None:
                if state & 0x1:
                    finfo["crc_ok"] = True
                elif state & 0x2:
                    finfo["crc_ok"] = False
                if state & 0x4:
                    finfo["file_version"] = 2
                elif state & 0x8:
                    finfo["file_version"] = 3
                elif state & 0x10:
                    finfo["file_version"] = 4
                elif state & 0x20:
                    finfo["file_version"] = 5
                else:
                    finfo["file_version"] = 1
                if state & 0x40:
                    finfo["censored"] = False
                elif state & 0x80:
                    finfo["censored"] = True

        if self._path:
            finfo["path"] = self._path
            finfo["size"] = self._size
            finfo["ed2khash"] = self._ed2khash
            finfo["mtime"] = self._mtime

        if "fid" in finfo:
            self._fid = finfo["fid"]
        if "lid" in finfo:
            self._lid = finfo["lid"]
        if "epno" in finfo:
            del finfo["epno"]

        if update_mylist:
            finfo["mylist_state"] = self.db_data.mylist_state
            finfo["mylist_viewed"] = self.db_data.mylist_viewed
            finfo["mylist_viewdate"] = self.db_data.mylist_viewdate
            finfo["mylist_source"] = self.db_data.mylist_source
            finfo["mylist_other"] = self.db_data.mylist_other
            finfo["lid"] = None
            self.remove_from_mylist()
            self._is_generic = False

        finfo["part"] = self._part

        anidb_client.log.debug(f"fetching a db session to update {self}")
        if self.db_data and not self.db_data.aid and "aid" not in finfo:
            anime, episodes = self._guess_anime_ep_from_file()
            finfo["aid"] = _required(anime, "anime").aid
            if not self.db_data.eid and "eid" not in finfo:
                finfo["eid"] = _required(episodes, "episodes")[0].eid

        try:
            with self._db_session() as sess:
                if self.db_data:
                    self.db_data = sess.merge(self.db_data)
                    anidb_client.log.debug(f"{self}: update {finfo}")
                    self.db_data.update(**finfo)
                    self.db_data.updated = datetime.datetime.now(self._timezone)
                else:
                    new = FileTable(**finfo)
                    new.updated = datetime.datetime.now(self._timezone)
                    new.last_update_dice = datetime.datetime.now(self._timezone)
                    sess.add(new)

                if new:
                    self.db_data = new
                self._db_commit(sess)
        except sqlalchemy.exc.OperationalError:
            anidb_client.log.error(f"Failed to update {self} in database")
        self._file_updated.set()

        if update_mylist:
            self.update_mylist(
                state=self.db_data.mylist_state,
                watched=self.db_data.mylist_viewdate,
                source=self.db_data.mylist_source,
                other=self.db_data.mylist_other,
            )

    def _anidb_mylist_data_callback(self, res: Response) -> None:
        new = None
        if res.rescode == "312":
            self._mylist_updated.set()
            raise AniDBFileError("anidb-client currently does not support multiple mylist entries for a single episode")
        elif res.rescode == "321":
            self._mylist_updated.set()
            return
        else:
            finfo: dict[str, Any] = res.datalines[0]
            if "date" in finfo:
                del finfo["date"]
            for attr, data in finfo.items():
                finfo[attr] = anidb_client.mapper.mylist_map_converters[attr](data)

        if "mylist_viewdate" in finfo and finfo["mylist_viewdate"]:
            finfo["mylist_viewed"] = True

        try:
            with self._db_session() as sess:
                if (self.db_data and self.db_data.is_generic and finfo["gid"]) or (
                    self.db_data and not self.db_data.is_generic and finfo["fid"] != self.db_data.fid
                ):
                    if finfo["gid"]:
                        finfo["is_generic"] = False
                    else:
                        finfo["is_generic"] = True

                    # there is something in mylist; but it's not us :/
                    existing = sess.query(FileTable).filter_by(lid=finfo["lid"]).all()
                    if not existing:
                        new = FileTable(**finfo)
                        new.updated = datetime.datetime.now(self._timezone)
                        new.last_update_dice = datetime.datetime.now(self._timezone)
                        sess.add(new)
                    else:
                        obj = existing[0]
                        obj.updated = datetime.datetime.now(self._timezone)
                        obj.last_update_dice = datetime.datetime.now(self._timezone)
                        obj.update(**finfo)
                    self._db_commit(sess)
                    self._mylist_updated.set()
                    return

                if self._path:
                    finfo["path"] = self._path
                    finfo["size"] = self._size
                    finfo["ed2khash"] = self._ed2khash
                    finfo["mtime"] = self._mtime

                finfo["part"] = self._part

                if finfo["gid"]:
                    self._is_generic = False
                else:
                    self._is_generic = True
                finfo["is_generic"] = self._is_generic
                if self.db_data:
                    anidb_client.log.debug(f"New mylist info: {finfo}")
                    self.db_data = sess.merge(self.db_data)
                    self.db_data.update(**finfo)
                    self.db_data.updated = datetime.datetime.now(self._timezone)
                else:
                    new = FileTable(**finfo)
                    new.updated = datetime.datetime.now(self._timezone)
                    new.last_update_dice = datetime.datetime.now(self._timezone)
                    anidb_client.log.debug(f"Adding mylist info: {finfo}")
                    sess.add(new)

                if new:
                    self.db_data = new
                self._db_commit(sess)
        except sqlalchemy.exc.OperationalError:
            anidb_client.log.error(f"Failed to update {self} in database")
        self._mylist_updated.set()

    def _send_anidb_update_req(self, prio: bool = False, req_mylist: bool = False, req_file: bool = True) -> None:
        anidb_client.log.debug(f"updating - fid: {self._fid}, size: {self._size}, path: {self._path}")
        # One name for both halves of this method: it sends a FILE request and then,
        # depending on what came back, a MYLIST one.
        req: Command
        if req_file:
            if self._fid:
                self._file_updated.clear()
                anidb_client.log.debug("sending file request with fid")
                req = FileCommand(
                    fid=self._fid,
                    fmask=anidb_client.mapper.getFileBitsF(anidb_client.mapper.file_map_f),
                    amask=anidb_client.mapper.getFileBitsA(["epno"]),
                )
                self._link().request(req, self._anidb_file_data_callback, prio=prio)
                self._file_updated.wait()
            elif self._size and self._path:
                self._file_updated.clear()
                anidb_client.log.debug("sending file request with size and hash")
                req = FileCommand(
                    size=self._size,
                    ed2k=self.ed2khash,
                    fmask=anidb_client.mapper.getFileBitsF(anidb_client.mapper.file_map_f),
                    amask=anidb_client.mapper.getFileBitsA(["epno"]),
                )
                self._link().request(req, self._anidb_file_data_callback, prio=prio)
                self._file_updated.wait()

        # We want to send a mylist request only if explicitly asked for, or if
        # we didn't get a fid from the File request
        if req_mylist or not self.db_data or not self.db_data.fid:
            if self._fid:
                anidb_client.log.debug("fetching mylist with fid")
                req = MyListCommand(fid=self._fid)
            elif self._lid:
                anidb_client.log.debug("fetching mylist with lid")
                req = MyListCommand(lid=self._lid)
            else:
                anidb_client.log.debug("fetching mylist with aid and epno")
                req = MyListCommand(aid=self.anime.aid, epno=self.episode.episode_number)
            anidb_client.log.debug("sending mylist request")
            self._link().request(req, self._anidb_mylist_data_callback, prio=prio)
            self._mylist_updated.wait()
        self._updating.release()

    def __repr__(self) -> str:
        db_data = object.__getattribute__(self, "db_data")
        path = object.__getattribute__(self, "_path")
        watched = db_data.mylist_viewdate if db_data else None
        filename = os.path.basename(path) if path else None
        return "File(filename='{}', episode={}, generic={}, watched={})".format(
            filename,
            object.__getattribute__(self, "_episode"),
            object.__getattribute__(self, "_is_generic"),
            watched,
        )

    def remove_from_mylist(self) -> None:
        wait = threading.Event()

        def _mylistdel_callback(res: Response) -> None:
            if res.rescode == "211":
                anidb_client.log.info(f"File {self} removed from mylist")
            elif res.rescode == "411":
                anidb_client.log.warning(f"File {self} was not in mylist")
            wait.set()

        if self.db_data and self.db_data.fid:
            req = MyListDelCommand(fid=self.db_data.fid)
            self._link().request(req, _mylistdel_callback, prio=True)
        elif self.db_data and self.db_data.lid:
            req = MyListDelCommand(lid=self.db_data.lid)
            self._link().request(req, _mylistdel_callback, prio=True)
        elif self.is_generic:
            # `self.is_generic`, not `self._is_generic`. The private attribute is
            # only set when the File was constructed as generic in this process; a
            # generic entry loaded from the cache leaves it None, so this branch was
            # skipped and the else below built MYLISTDEL with size and ed2k -- both
            # None for a generic file -- which the command rejects outright. The add
            # path already reads the public property.
            # `_expanded_epno`, not the raw episode number: a ranged epno reached
            # here unexpanded and sent one MYLISTDEL for "5-7", which matches no
            # entry AniDB holds -- so the removal reported success and removed
            # nothing, while the symmetric add loop had created three entries.
            #
            # And `_expanded_epno`, not the `multiep` property, which is the other
            # half of that asymmetry and deliberately kept: the property may also
            # adopt the episode set guessed from the filename, and a filename must
            # not get to decide what is deleted from someone's mylist. Expanding the
            # range is the whole of the reported defect (#12); the filename branch
            # is a behaviour change and wants its own evidence.
            episodes = self._multiep or _expanded_epno(self.episode.episode_number)
            for ep in episodes:
                wait.clear()
                req = MyListDelCommand(aid=_required(self._anime, "anime").aid, epno=ep)
                self._link().request(req, _mylistdel_callback, prio=True)
                wait.wait()
        else:
            req = MyListDelCommand(size=self.size, ed2k=self.ed2khash)
            self._link().request(req, _mylistdel_callback, prio=True)
        self._lid = None
        finfo = {
            "mylist_state": None,
            "mylist_filestate": None,
            "mylist_viewed": None,
            "mylist_viewdate": None,
            "mylist_storage": None,
            "mylist_source": None,
            "mylist_other": None,
            "lid": None,
        }
        with self._db_session() as sess:
            self.db_data = sess.merge(self.db_data)
            self.db_data.update(**finfo)
            self._db_commit(sess)
        wait.wait()

    def update_mylist(
        self,
        state: str | None = None,
        watched: bool | datetime.datetime | None = None,
        source: str | None = None,
        other: str | None = None,
    ) -> None:
        wait = threading.Event()
        self.update_if_old()
        viewdate = None
        edit = False
        req = None

        def _mylistadd_callback(res: Response) -> None:
            if res.rescode in ("320", "330", "350", "310", "322", "411"):
                anidb_client.log.warning(f"Could not add file {self} to mylist, anidb says: {res.rescode}")
            elif res.rescode in ("210", "310", "311"):
                # if 'entrycnt' is > 1 this is actually the lid...
                # ... which is good I guess, because we want it.
                anidb_client.log.debug(f"lines from MYLISTADD command: {res.datalines}")
                # The count gets its own name. It used to be assigned back over `res`
                # itself, so when the reply carried neither field -- or carried no
                # data lines at all -- the comparison below ran against the Response
                # object and raised. On the response thread that is not a lost id, it
                # is a skipped wait.set() and a caller blocked for good.
                line = res.datalines[0] if res.datalines else {}
                entry_count = int(line.get("entries") or line.get("entrycnt") or 0)
                if entry_count > 1:
                    # More than one entry means the field was really the lid, which
                    # is the thing we actually wanted.
                    with self._db_session() as sess:
                        self.db_data = sess.merge(self.db_data)
                        self.db_data.update(lid=entry_count)
                        self._db_commit(sess)
            wait.set()

        try:
            state_num = [x for x, y in anidb_client.mapper.mylist_state_map.items() if y == state][0]
        except IndexError:
            state_num = None

        if watched:
            if isinstance(watched, datetime.datetime):
                viewdate = int(watched.timestamp())
            viewed = 1
        elif watched is False:
            viewed = 0
        else:
            viewed = None

        # Make sure this episode isn't already in mylist
        if not self.lid:
            # avoid a lookup call if we have a file in our database
            with self._db_session() as sess:
                res = sess.query(FileTable).filter_by(eid=self.episode.eid).all()
                self._db_commit(sess)
            mylist_entries = [x for x in res if x.lid]
            if mylist_entries:
                for entry in mylist_entries:
                    other_file = File(lid=entry.lid)
                    other_file.remove_from_mylist()
            else:
                # Nothing in local database; ask the API
                other_file = File(anime=self.anime, episode=self.episode)
                if other_file.lid:
                    other_file.remove_from_mylist()

        if self.lid:
            edit = True
            req = MyListAddCommand(
                lid=self.db_data.lid,
                edit=1,
                state=state_num,
                viewed=viewed,
                viewdate=viewdate,
                source=source,
                other=other,
            )
        elif self.fid:
            req = MyListAddCommand(
                fid=self.fid, state=state_num, viewed=viewed, viewdate=viewdate, source=source, other=other
            )
        elif self.is_generic or not self._path:
            # One MYLISTADD per episode, sent inside the loop. The send used to sit
            # after the whole if/elif chain, so a generic file covering several
            # episodes built a command for each and then sent only the last one --
            # every episode but one was silently missing from mylist. The exact
            # mirror of the deletion loop, which sent once per episode but always
            # named the same one.
            for ep in self.multiep:
                wait.clear()
                req = MyListAddCommand(
                    aid=_required(self._anime, "anime").aid,
                    epno=ep,
                    generic=1,
                    state=state_num,
                    viewed=viewed,
                    viewdate=viewdate,
                    source=source,
                    other=other,
                )
                self._link().request(req, _mylistadd_callback, prio=True)
                wait.wait()
            # Already sent; nothing left for the single send below. This also stops
            # an empty episode list reaching it with req still None.
            req = None
        else:
            req = MyListAddCommand(
                size=self.size,
                ed2k=self.ed2khash,
                state=state_num,
                viewed=viewed,
                viewdate=viewdate,
                source=source,
                other=other,
            )
        if req is not None:
            self._link().request(req, _mylistadd_callback, prio=True)
            wait.wait()
        if edit:
            with self._db_session() as sess:
                self.db_data = sess.merge(self.db_data)
                if state:
                    self.db_data.mylist_state = state
                if watched:
                    self.db_data.mylist_viewed = True
                    if isinstance(watched, datetime.datetime):
                        self.db_data.mylist_viewdate = watched
                    else:
                        self.db_data.mylist_viewdate = datetime.datetime.now(self._timezone)
                else:
                    self.db_data.mylist_viewed = False
                    self.db_data.mylist_viewdate = None
                if source:
                    self.db_data.mylist_source = source
                if other:
                    self.db_data.mylist_other = other
                self._db_commit(sess)
        else:
            # Oh lord, another slowdown?
            # Sorry, since anidb doesn't return our lid and eid when adding we
            # have to do another request here...
            locked = self._updating.acquire(False)
            if not locked:
                self._updating.acquire()
                self._updating.release()
                return
            self._send_anidb_update_req(req_file=False, req_mylist=True)
        anidb_client.log.info(f"File {self} updated in mylist")

    def _guess_anime_ep_from_file(self, aid: int | None = None) -> tuple[Anime | None, list[Episode] | None]:
        # Read the path without going through __getattr__. This runs from inside
        # _anidb_file_data_callback, and `self.path` on a File with no local path
        # falls through to update_if_old() -- which starts a fetch and waits on the
        # very event this callback exists to set. A File built from an fid AniDB
        # does not have deadlocked there permanently.
        path = self._path or (self.db_data.path if self.db_data else None)
        if not path:
            return (None, None)
        head, filename = os.path.split(path)
        head, parent_dir = os.path.split(head)

        if not aid:
            # first try to figure out anime by the directory name
            if parent_dir and self.parse_dir:
                series = anidb_client.anames.get_titles(name=parent_dir)
                if series:
                    aid = series[0][0]
                    anidb_client.log.debug(f"dir '{parent_dir}': score {series[0][2]} for '{series[0][3]}'")
                else:
                    anidb_client.log.debug(f"dir '{parent_dir}': no match")

            # no confident hit on parent directory, trying filename
            if not aid:
                # strip away all kinds of parenthesis like
                # [<group>], (<codec>) or {<crc>}.
                stripped = re.sub(r"[{[(][^\]})]*?[})\]]", "", filename)
                # Remove episode numbers
                stripped = re.sub(r"-[ _]?\d+(-\d+)?", "", stripped)
                stripped = re.sub(r"EP?(isode)?[ _]?\d+(-\d+)?", "", stripped, flags=re.IGNORECASE)
                # remove the file ending
                stripped, tail = stripped.rsplit(".", 1)
                # split out all words, this removes all dots, dashes and other
                # unhealthy things :)
                # Don't know if I should remove numbers here as well...
                words = re.findall(r"[\w]+", stripped)
                # Join back to a single string
                joined = " ".join(words)
                # search anidb, but require lower score for match as this is
                # probably not very similar to the real title...
                series = anidb_client.anames.get_titles(name=joined, score_for_match=0.5)
                if series:
                    anidb_client.log.debug(
                        f"file '{filename}': trimmed to '{joined}', score {series[0][2]} for '{series[0][3]}'"
                    )
                    aid = series[0][0]
                else:
                    anidb_client.log.debug(f"file '{filename}': trimmed to '{joined}', no match")
            if not aid:
                return (None, None)

        anime = Anime(aid)
        episodes = self._guess_epno_from_filename(filename, anime)

        return (anime, episodes)

    def _search_filename(self, filename: str, regex: re.Pattern[str], anime: Anime) -> list[str]:
        ret = []
        res = regex.search(filename)
        if res:
            eps = anidb_client.fileinfo.multiep_re.findall(res.group(3))
            eps.insert(0, res.group(2))
            for m in eps:
                try:
                    ep = int(m)
                except ValueError:
                    if not m:
                        # The specials, opening, ending and trailer patterns all
                        # allow the number to be absent -- "foo.special.mkv",
                        # "foo.NCOP.mkv" -- which is what an empty capture means
                        # here. The first one of its kind is the only sensible
                        # reading, so treat it as number 1.
                        ep = 1
                    elif m.lower() in anidb_client.mapper.roman_numbering:
                        # `m`, not `ep`. This branch read `ep`, which on the first
                        # iteration is unbound (int() had just failed) and on later
                        # ones holds the *previous* episode number -- so it raised
                        # UnboundLocalError or AttributeError rather than ever
                        # converting a numeral.
                        ep = anidb_client.mapper.roman_numbering[m.lower()]
                    else:
                        anidb_client.log.warning(
                            f"Got non-numeric episode number when searching '{filename}' with regex '{regex}'"
                        )
                        continue
                if res.group(1).lower() in ("s", "0", "00"):
                    ret.append(f"S{ep}")
                elif res.group(1).lower() == "o":
                    ret.append(f"C{ep}")
                elif res.group(1).lower() == "e":
                    # This is error prone, but we're guessing that endings
                    # starts at half the credits count...
                    count = anime.credit_count
                    if count:
                        start = int(count / 2)
                        ep = start + ep
                    ret.append(f"C{ep}")
                elif res.group(1).lower() in ("t", "pv"):
                    ret.append(f"T{ep}")
                else:
                    ret.append(str(ep))
        return ret

    def _guess_epno_from_filename(self, filename: str, anime: Anime) -> list[Episode]:
        count = 1
        ret = None
        for r in anidb_client.fileinfo.ep_nr_re:
            # abort when we reach the fallback regex; represented by a None
            # entry in the array
            if not r:
                break
            count += 1
            ret = self._search_filename(filename, r, anime)
            if ret:
                break
        if not ret:
            if self.force_single_episode_series:
                # We assume that this file belongs to an anime with just a
                # single episode
                return [Episode(anime=anime, epno=1)]
            else:
                # if this series/movie/ova only has one regular episode, we claim
                # this is it.
                if anime.nr_of_episodes == 1:
                    return [Episode(anime=anime, epno=1)]

            # multi episode series, but the regular regexp gave nothing, try
            # the fallbacks
            for r in anidb_client.fileinfo.ep_nr_re[count:]:
                # The slice starts past the None sentinel the loop above stopped at,
                # so this never fires -- it is what makes that readable to a checker.
                if r is None:
                    continue
                ret = self._search_filename(filename, r, anime)
                if ret:
                    break
            if not ret:
                anidb_client.log.debug(f"file '{filename}': could not figure out episode number(s)")
                return []
        first = re.match(anidb_client.fileinfo.specials_re, ret[0])
        if len(ret) == 2:
            if first:
                # _search_filename prefixes every number it found from one match's
                # group(1), so both endpoints of a range always carry the same
                # prefix. The second match is still checked rather than assumed:
                # reading .group() off a failed match would raise here, halfway
                # through building the episode list.
                second = re.match(anidb_client.fileinfo.specials_re, ret[1])
                if second:
                    mi = int(first.group(2))
                    ma = int(second.group(2))
                    ret = [f"{first.group(1).upper()}{x}" for x in range(mi, ma + 1)]
            else:
                mi = int(ret[0])
                ma = int(ret[1])
                ret = [str(x) for x in range(mi, ma + 1)]
        anidb_client.log.debug(f"file '{filename}': looks like episode(s) {ret}")
        return [Episode(anime=anime, epno=e) for e in ret]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, File):
            return NotImplemented
        if self.fid and self.fid == other.fid:
            return True
        if self._is_generic and other.is_generic:
            return self.episode == other.episode
        # Two real files with different fids are not equal. Falling off the end here
        # returned None, which is falsy and so read as "not equal" most of the time,
        # but is not a bool: `bool(a == b)` and `a != b` both worked by accident, and
        # anything inspecting the result itself saw None.
        return False

    def __len__(self) -> int:
        return len(self.multiep)

    def __contains__(self, other: object) -> bool:
        # See Anime.__contains__: NotImplemented is not a legal answer here, and
        # since 3.14 returning it raises TypeError rather than reading as true.
        if not isinstance(other, Episode):
            return False
        return other.episode_number in self.multiep


class Group(AniDBObj):
    _gid: int | None = None
    _name: str | None = None

    def __init__(self, name: str | None = None, gid: int | None = None) -> None:
        super().__init__()
        if not (name or gid):
            raise IllegalAnimeObject("At least name or gid must be given when creating a Group object")

        if gid:
            self._gid = gid
        if name:
            self._name = name
        self.db_data = None
        self._get_db_data()

    def _anidb_data_callback(self, res: Response) -> None:
        with self._db_session() as sess:
            if self.db_data:
                self.db_data = sess.merge(self.db_data)

            if res.rescode == "350":
                if self.db_data:
                    sess.delete(self.db_data)
                if self._name:
                    new = GroupTable(
                        name=self._name,
                        short=self._name,
                        updated=datetime.datetime.now(self._timezone),
                        last_update_dice=datetime.datetime.now(self._timezone),
                    )
                    sess.add(new)
            else:
                ginfo: dict[str, Any] = res.datalines[0]
                for attr, data in ginfo.items():
                    if attr == "relations":
                        relations = _group_relation_rows(data.split("'"))

                        if self.db_data:
                            new_relations = []
                            for r in relations:
                                found = False
                                for sr in self.db_data.relations:
                                    if r.related_gid == sr.related_gid:
                                        found = True
                                        sr.relation_type = r.relation_type
                                        sr.group_pk = self.db_data.pk
                                        new_relations.append(sr)
                                if not found:
                                    r.group_pk = self.db_data.pk
                                    new_relations.append(r)
                            for r in self.db_data.relations:
                                if r not in new_relations:
                                    sess.delete(r)
                            relations = new_relations

                        ginfo["relations"] = relations
                    elif attr in anidb_client.mapper.group_map_converters:
                        ginfo[attr] = anidb_client.mapper.group_map_converters[attr](data)

            if self.db_data:
                self.db_data.update(**ginfo)
                self.db_data.updated = datetime.datetime.now(self._timezone)
                new_relations = []
                for r in ginfo["relations"]:
                    found = False
                    for sr in self.db_data.relations:
                        if r.related_gid == sr.related_gid:
                            found = True
                            sr.relation_type = r.relation_type
                            sr.group_pk = self.db_data.pk
                            new_relations.append(sr)
                    if not found:
                        r.group_pk = self.db_data.pk
                        new_relations.append(r)
                for r in self.db_data.relations:
                    if r not in new_relations:
                        sess.delete(r)
                self.db_data.relations = new_relations
            else:
                new = GroupTable(**ginfo)
                new.updated = datetime.datetime.now(self._timezone)
                new.last_update_dice = datetime.datetime.now(self._timezone)
                sess.add(new)
                self.db_data = new

            self._db_commit(sess)
        self._updated.set()

    def _get_db_data(self) -> None:
        with self._db_session() as sess:
            if self._gid:
                res = sess.query(GroupTable).filter_by(gid=self._gid).all()
            else:
                res = (
                    sess.query(GroupTable)
                    .filter(sqlalchemy.or_(GroupTable.name.ilike(self._name), GroupTable.short.ilike(self._name)))
                    .all()
                )
            if len(res) > 0:
                self.db_data = res[0]
                anidb_client.log.debug(f"Found db_data for group: {self.db_data}")

    @override
    def _send_anidb_update_req(self, prio: bool = False) -> None:
        self._updated.clear()
        req = GroupCommand(gid=self._gid) if self._gid else GroupCommand(gname=self._name)
        self._link().request(req, self._anidb_data_callback, prio=prio)
        self._updated.wait()
        self._updating.release()

    def __repr__(self) -> str:
        return "Group(gid='{}', name='{}')".format(
            object.__getattribute__(self, "_gid"), object.__getattribute__(self, "_name")
        )
