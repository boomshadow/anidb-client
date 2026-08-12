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


import datetime
import enum
from collections.abc import Iterable
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Unicode,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

import anidb_client

# The constrained vocabularies, defined once.
#
# These used to be written out twice -- as bare strings in the Enum() columns
# below, and again as the values of the conversion tables in mapper.py -- with
# nothing keeping the two in step. Adding a value to one and not the other
# produced either a row the database rejects or a wire value converting to a
# string no column accepts, and neither showed up until it happened.
#
# They live here because the schema is the self-enforcing artifact: these classes
# are executed to build the columns, so the vocabulary cannot be described
# incorrectly. mapper.py maps AniDB's wire codes onto the members.
#
# StrEnum rather than Enum: members are drop-in for the strings they replace --
# equal to them, hashing as them, and formatting as them -- so callers holding a
# plain "on hdd" keep working and the value stored in the database is unchanged.


class AnimeRelationType(enum.StrEnum):
    SEQUEL = "sequel"
    PREQUEL = "prequel"
    SAME_SETTING = "same setting"
    ALTERNATIVE_SETTING = "alternative setting"
    ALTERNATIVE_VERSION = "alternative version"
    MUSIC_VIDEO = "music video"
    CHARACTER = "character"
    SIDE_STORY = "side story"
    PARENT_STORY = "parent story"
    SUMMARY = "summary"
    FULL_STORY = "full story"
    OTHER = "other"


class EpisodeType(enum.StrEnum):
    REGULAR = "regular"
    SPECIAL = "special"
    CREDIT = "credit"
    TRAILER = "trailer"
    PARODY = "parody"
    OTHER = "other"


class MylistState(enum.StrEnum):
    UNKNOWN = "unknown"
    ON_HDD = "on hdd"
    ON_CD = "on cd"
    DELETED = "deleted"


class MylistFileState(enum.StrEnum):
    NORMAL_ORIGINAL = "normal/original"
    CORRUPTED = "corrupted version/invalid crc"
    SELF_EDITED = "self edited"
    SELF_RIPPED = "self ripped"
    ON_DVD = "on dvd"
    ON_VHS = "on vhs"
    ON_TV = "on tv"
    IN_THEATERS = "in theaters"
    STREAMED = "streamed"
    OTHER = "other"


class GroupRelationType(enum.StrEnum):
    PARTICIPANT_IN = "participant in"
    PARENT_OF = "parent of"
    MERGED_FROM = "merged from"
    NOW_KNOWN_AS = "now known as"
    OTHER = "other"
    INCLUDES = "includes"
    FORMERLY = "formerly"
    MERGED_INTO = "merged into"
    LOST_PART = "lost part"
    SPLIT_FROM = "split from"
    CHILD_OF = "child of"


def _values(members: Iterable[enum.Enum]) -> list[str]:
    """Persist a StrEnum by value, not by member name.

    SQLAlchemy's default for a Python enum is to store `member.name`, which would
    put ON_HDD in a column that has always held "on hdd". These vocabularies are
    AniDB's wording, punctuation and all -- "normal/original" is not a legal
    identifier -- so the value is the only faithful thing to store.
    """
    return [str(member.value) for member in members]


class Base(DeclarativeBase):
    """Declarative base for the cache schema.

    The class form rather than `Base = declarative_base()`: the function returns a
    value with no static type, so every model inheriting from it was an "invalid
    base class" to a type checker and none of the mapped attributes could be
    inferred. (The function itself also moved packages in SQLAlchemy 2.0 -- it was
    previously imported from the deprecated sqlalchemy.ext.declarative, which
    emitted MovedIn20Warning on import.)

    Columns are declared `Mapped[T]` / `mapped_column(...)`. The annotation is what
    a type checker reads off a row -- `str`, not `Column[str]` -- which is the whole
    reason for the style: under the legacy form every read was a column object and
    every direct assignment an error, so callers routed writes through a setattr
    helper and annotated reads `Any` to get past the checker. Those workarounds are
    gone.

    **Nullability is stated, never inferred.** SQLAlchemy will take it from the
    annotation -- `Mapped[str | None]` implying NULL -- and that is deliberately not
    relied on here: every `mapped_column()` passes `nullable=` outright, so the DDL
    is decided by one thing rather than by two that can disagree. This cache has no
    migration story (SPEC-003), so a nullability that changed because an annotation
    was edited would cost every user a rebuild. The DDL snapshot in
    `tests/schema_snapshots/` is what proves it did not.
    """


# How many connections the cache pool keeps, and how many more it may open in a
# burst before refusing. The ceiling matters more than the numbers: this library
# runs inside somebody else's application, and the pool used to be created with
# max_overflow=-1 -- unlimited, "never block, unconditionally make a new connection"
# -- so a connection leak grew until PostgreSQL's max_connections or the process's
# file-descriptor limit refused somebody, quite possibly the host application
# rather than us. A bound turns that into a fast, attributable error instead. A
# caller who knows its own concurrency overrides the size through init().
DEFAULT_POOL_SIZE = 10
POOL_MAX_OVERFLOW = 5

# How long SQLite waits behind another writer before raising "database is locked",
# in seconds. Set rather than inherited: the driver's default five seconds is a
# value nobody here decided, and WAL makes contention rarer without removing it.
#
# Passed as the driver's own `timeout` connect argument, which is exactly SQLite's
# busy timeout, rather than as a `PRAGMA busy_timeout=` statement -- a pragma takes
# no bound parameter, so setting it in SQL means formatting the value into the
# statement, which is the shape of a SQL-injection bug even when the value is a
# constant defined here.
SQLITE_BUSY_TIMEOUT_SECONDS = 15.0


def _is_sqlite(url: str) -> bool:
    try:
        return make_url(url).drivername.startswith("sqlite")
    except ArgumentError:
        return False


def is_in_memory_sqlite(url: str) -> bool:
    """True when the URL names a SQLite database that exists only in memory.

    Both spellings count -- `sqlite://` and `sqlite:///:memory:` -- as does the
    URI form naming `mode=memory`, whose parameters SQLAlchemy lifts out of the
    path into the URL's query. Anything that is not SQLite, and anything this
    cannot parse, is answered False: the question is only asked to refuse a
    configuration that cannot work, so a URL that will not parse is left to fail where it
    is actually opened.
    """
    if not _is_sqlite(url):
        return False
    parsed = make_url(url)
    database = parsed.database or ""
    return not database or database == ":memory:" or parsed.query.get("mode") == "memory"


def _enforce_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """Turn foreign-key enforcement on as each SQLite connection is opened.

    SQLite ignores foreign keys unless each connection turns them on, so the
    constraints this schema declares are decorative without this listener: the
    cascade tests pass on SQLAlchemy performing the cascade in Python, not on the
    database refusing anything. Enforcement is a per-connection setting, which is
    why it goes on the connect event rather than being done once at startup.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _request_wal(engine: Engine) -> str | None:
    """Ask SQLite for write-ahead logging; answer with the mode actually in force.

    `PRAGMA journal_mode=WAL` returns the mode that resulted, so no filesystem
    detection is needed to find out whether the request was granted. None means
    the statement itself failed, which some filesystems do rather than answering
    with the unchanged mode.
    """
    try:
        with engine.connect() as conn:
            return str(conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar())
    except OperationalError:
        return None


def _configure_sqlite(engine: Engine) -> None:
    """Bring a SQLite cache up to what this library's threading model needs.

    WAL is attempted rather than required. It does not work over a network
    filesystem -- it needs shared memory between the processes on one host -- and
    this package supports `nfs://` paths, so a cache on a NAS is a configuration
    somebody has. It also creates `-wal` and `-shm` companion files next to a
    database file the user owns. So the mode is asked for, the answer is read, and
    a refusal is logged and carried on from rather than raised.
    """
    event.listen(engine, "connect", _enforce_foreign_keys)
    mode = _request_wal(engine)
    # init_db() is reachable before init() has installed a logger -- a caller's own
    # test suite opens the cache directly -- so the logger is checked, as in mapper.py.
    if anidb_client.log is None:
        return
    if mode is None:
        anidb_client.log.warning("SQLite refused the journal-mode change; the cache keeps the mode it had")
    elif mode.lower() == "wal":
        anidb_client.log.debug("SQLite cache journal mode is WAL")
    else:
        anidb_client.log.info(f"SQLite cache journal mode is {mode!r}; WAL was asked for and not granted")


def init_db(url: str, pool_size: int = DEFAULT_POOL_SIZE) -> sessionmaker[Session]:
    # Connection-pool sizing is only meaningful for pools that queue. SQLAlchemy
    # gives in-memory SQLite a SingletonThreadPool, which takes neither argument
    # and raises TypeError if handed them -- so an in-memory cache, the obvious
    # choice for a caller's own test suite, could not be opened at all. File-backed
    # SQLite and the server databases all get a QueuePool and are unaffected.
    connect_args: dict[str, Any] = {}
    if _is_sqlite(url):
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_SECONDS
    pool_options = {"pool_size": pool_size, "max_overflow": POOL_MAX_OVERFLOW}
    try:
        engine = create_engine(url, connect_args=connect_args, **pool_options)
    except TypeError:
        engine = create_engine(url, connect_args=connect_args)
    # SQLite only. The pragma is not valid SQL anywhere else, and the defaults it
    # corrects are SQLite's alone.
    if engine.dialect.name == "sqlite":
        _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    return session


class AnimeTable(Base):
    __tablename__ = "anime"

    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    aid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, unique=True)
    # TODO dateflags?
    year: Mapped[str] = mapped_column(String(16), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)

    nr_of_episodes: Mapped[int] = mapped_column(Integer, nullable=False)
    highest_episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    special_ep_count: Mapped[int] = mapped_column(Integer, nullable=False)
    air_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    picname: Mapped[str | None] = mapped_column(String(128), nullable=True)

    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    vote_count: Mapped[int] = mapped_column(Integer, nullable=False)
    temp_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_vote_count: Mapped[int] = mapped_column(Integer, nullable=False)
    average_review_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_18_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False)

    ann_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    allcinema_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    animenfo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    anidb_updated: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    special_count: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    other_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trailer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parody_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # TODO: ANIMEDESC
    # description: Mapped[str | None] = mapped_column(Unicode(8194), nullable=True)

    updated: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_update_dice: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    relations: Mapped[list[AnimeRelationTable]] = relationship(
        "AnimeRelationTable", backref="anime", cascade="all, delete"
    )

    def update(self, **kwargs: Any) -> None:
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self) -> str:
        return (
            f"<AnimeTable(pk={self.pk}, aid={self.aid}, episodes={self.nr_of_episodes}, "
            f"highest_episode_number={self.highest_episode_number}, updated="
            f"{self.updated})>"
        )


class AnimeRelationTable(Base):
    __tablename__ = "anime_relation"

    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    anime_pk: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("anime.pk"), nullable=False
    )
    related_aid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    relation_type: Mapped[AnimeRelationType] = mapped_column(
        Enum(AnimeRelationType, values_callable=_values, name="anime_relation_type_enum"),
        nullable=False,
    )

    # Deliberately no __eq__: these rows compare by identity, which is what the
    # refresh loop in animeobjs.py wants. That loop appends the very row objects
    # it matched into its replacement list, so `row not in new_relations` is
    # asking "is this one I kept", and the default identity comparison answers it
    # correctly. Defining __eq__ would also drop the class's __hash__, and
    # SQLAlchemy keeps mapped instances in sets.
    #
    # A Python 2 `__cmp__` used to sit here comparing the three columns by value.
    # Python 3 never calls __cmp__, so it did nothing at all -- but it read as
    # though equality were value-based, which is the opposite of the truth.

    def __repr__(self) -> str:
        return (
            f"<AnimeRelationTable(pk={self.pk}, anime_pk={self.anime_pk}, related_aid={self.related_aid}, "
            f"type={self.relation_type})>"
        )


class EpisodeTable(Base):
    __tablename__ = "episode"

    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    aid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    eid: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), nullable=False, unique=True, index=True
    )
    length: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    votes: Mapped[int] = mapped_column(Integer, nullable=False)
    epno: Mapped[str] = mapped_column(String(8), nullable=False)
    title_eng: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_romaji: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_kanji: Mapped[str | None] = mapped_column(Unicode(512), nullable=True)
    aired: Mapped[datetime.date | None] = mapped_column(Date(), nullable=True)
    type: Mapped[EpisodeType] = mapped_column(
        Enum(EpisodeType, values_callable=_values, name="episode_type_enum"), nullable=False
    )

    updated: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_update_dice: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs: Any) -> None:
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self) -> str:
        return (
            f"<EpisodeTable(pk={self.pk}, aid={self.aid}, epno={self.epno}, "
            f"title_eng={self.title_eng}, updated={self.updated})>"
        )


class FileTable(Base):
    __tablename__ = "file"

    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    path: Mapped[str | None] = mapped_column(Unicode(512), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    ed2khash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mtime: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    aid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    gid: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    eid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    fid: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True, index=True)
    is_deprecated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_generic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    part: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # state
    crc_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    file_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    censored: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    length_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aired_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    mylist_state: Mapped[MylistState | None] = mapped_column(
        Enum(MylistState, values_callable=_values, name="mylist_state_enum"), nullable=True
    )
    mylist_filestate: Mapped[MylistFileState | None] = mapped_column(
        Enum(MylistFileState, values_callable=_values, name="mylist_filestate_enum"),
        nullable=True,
    )
    mylist_viewed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mylist_viewdate: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    mylist_storage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mylist_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mylist_other: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lid: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)

    updated: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_dice: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs: Any) -> None:
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self) -> str:
        # Encoded on purpose: this repr goes into log messages, and a path is whatever
        # the filesystem holds, so it is rendered as an escaped byte string rather than
        # as characters a log consumer may not be able to write. `!r` says that is
        # intended -- formatting bytes gives the same text either way, and without it a
        # type checker rightly asks whether the b'...' was meant.
        path = self.path.encode("utf-8") if self.path else None
        return (
            f"<FileTable(pk={self.pk}, path={path!r}, mylist_state={self.mylist_state}, "
            f"mylist_viewed={self.mylist_viewed}, updated={self.updated})>"
        )


class GroupTable(Base):
    __tablename__ = "group"

    # Everything optional here was optional before by omission -- a bare `Column(...)`
    # is nullable -- and now says so. Same DDL; one fewer thing a reader has to know
    # the default of.
    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    gid: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True, index=True)
    rating: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    votes: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    acount: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    fcount: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    name: Mapped[str] = mapped_column(Unicode(248), nullable=False)
    short: Mapped[str] = mapped_column(Unicode(64), nullable=False, index=True)
    irc_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    irc_server: Mapped[str | None] = mapped_column(String(32), nullable=True)
    url: Mapped[str | None] = mapped_column(String(248), nullable=True)
    picname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    founded: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    disbanded: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    dateflag: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    last_release: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_activity: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    relations: Mapped[list[GroupRelationTable]] = relationship(
        "GroupRelationTable", backref="group", cascade="all, delete"
    )

    updated: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_dice: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs: Any) -> None:
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self) -> str:
        return f"<GroupTable(pk={self.pk}, gid={self.gid}, name={self.name}>"


class GroupRelationTable(Base):
    __tablename__ = "group_relation"

    pk: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    group_pk: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("group.pk"), nullable=False
    )
    related_gid: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    relation_type: Mapped[GroupRelationType] = mapped_column(
        Enum(GroupRelationType, values_callable=_values, name="group_relation_type_enum"),
        nullable=False,
    )

    # Identity equality, and no __eq__ -- see AnimeRelationTable above. The same
    # dead Python 2 __cmp__ used to sit here too.

    def __repr__(self) -> str:
        return (
            f"<GroupRelationTable(pk={self.pk}, group_pk={self.group_pk}, "
            f"related_gid={self.related_gid}, type={self.relation_type})>"
        )
