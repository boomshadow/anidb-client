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
from collections.abc import Iterable

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Unicode,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

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

    Columns stay in the legacy `Column(...)` style rather than being migrated to
    `mapped_column()`; that migration would touch every model and is not needed for
    the schema to be type-checked.
    """


def init_db(url):
    # Connection-pool sizing is only meaningful for pools that queue. SQLAlchemy
    # gives in-memory SQLite a SingletonThreadPool, which takes neither argument
    # and raises TypeError if handed them -- so an in-memory cache, the obvious
    # choice for a caller's own test suite, could not be opened at all. File-backed
    # SQLite and the server databases all get a QueuePool and are unaffected.
    engine_options = {"pool_size": 10, "max_overflow": -1}
    try:
        engine = create_engine(url, **engine_options)
    except TypeError:
        engine = create_engine(url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    return session


class AnimeTable(Base):
    __tablename__ = "anime"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    aid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, unique=True)
    # TODO dateflags?
    year = Column(String(16), nullable=False)
    type = Column(String(16), nullable=False)

    nr_of_episodes = Column(Integer, nullable=False)
    highest_episode_number = Column(Integer, nullable=False)
    special_ep_count = Column(Integer, nullable=False)
    air_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    url = Column(String(512), nullable=True)
    picname = Column(String(128), nullable=True)

    rating = Column(Float, nullable=True)
    vote_count = Column(Integer, nullable=False)
    temp_rating = Column(Float, nullable=True)
    temp_vote_count = Column(Integer, nullable=False)
    average_review_rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=False)
    is_18_restricted = Column(Boolean, nullable=False)

    ann_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    allcinema_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    animenfo_id = Column(String(64), nullable=True)
    anidb_updated = Column(DateTime(timezone=False), nullable=False)

    special_count = Column(Integer, nullable=False)
    credit_count = Column(Integer, nullable=False)
    other_count = Column(Integer, nullable=False)
    trailer_count = Column(Integer, nullable=False)
    parody_count = Column(Integer, nullable=False)

    # TODO: ANIMEDESC
    # description = Column(Unicode(8194), nullable=True)

    updated = Column(DateTime(timezone=True), nullable=False)
    last_update_dice = Column(DateTime(timezone=True), nullable=False)

    relations = relationship("AnimeRelationTable", backref="anime", cascade="all, delete")

    def update(self, **kwargs):
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self):
        return (
            f"<AnimeTable(pk={self.pk}, aid={self.aid}, episodes={self.nr_of_episodes}, "
            f"highest_episode_number={self.highest_episode_number}, updated="
            f"{self.updated})>"
        )


class AnimeRelationTable(Base):
    __tablename__ = "anime_relation"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    anime_pk = Column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("anime.pk"), nullable=False)
    related_aid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    relation_type: Column[str] = Column(
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

    def __repr__(self):
        return (
            f"<AnimeRelationTable(pk={self.pk}, anime_pk={self.anime_pk}, related_aid={self.related_aid}, "
            f"type={self.relation_type})>"
        )


class EpisodeTable(Base):
    __tablename__ = "episode"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    aid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    eid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, unique=True, index=True)
    length = Column(Integer, nullable=False)
    rating = Column(Float, nullable=True)
    votes = Column(Integer, nullable=False)
    epno = Column(String(8), nullable=False)
    title_eng = Column(String(512), nullable=True)
    title_romaji = Column(String(512), nullable=True)
    title_kanji = Column(Unicode(512), nullable=True)
    aired = Column(Date(), nullable=True)
    type: Column[str] = Column(Enum(EpisodeType, values_callable=_values, name="episode_type_enum"), nullable=False)

    updated = Column(DateTime(timezone=True), nullable=False)
    last_update_dice = Column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs):
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self):
        return (
            f"<EpisodeTable(pk={self.pk}, aid={self.aid}, epno={self.epno}, "
            f"title_eng={self.title_eng}, updated={self.updated})>"
        )


class FileTable(Base):
    __tablename__ = "file"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    path = Column(Unicode(512), nullable=True)
    size = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    ed2khash = Column(String(64), nullable=True)
    mtime = Column(DateTime(timezone=False), nullable=True)
    aid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    gid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    eid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    fid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True, index=True)
    is_deprecated = Column(Boolean, nullable=True)
    is_generic = Column(Boolean, nullable=False)
    part = Column(Integer, nullable=True)

    # state
    crc_ok = Column(Boolean, nullable=True)
    file_version = Column(Integer, nullable=True)
    censored = Column(Boolean, nullable=True)

    length_in_seconds = Column(Integer, nullable=True)
    description = Column(String(512), nullable=True)
    aired_date = Column(Date, nullable=True)

    mylist_state: Column[str] = Column(
        Enum(MylistState, values_callable=_values, name="mylist_state_enum"), nullable=True
    )
    mylist_filestate: Column[str] = Column(
        Enum(MylistFileState, values_callable=_values, name="mylist_filestate_enum"),
        nullable=True,
    )
    mylist_viewed = Column(Boolean, nullable=True)
    mylist_viewdate = Column(DateTime(timezone=False), nullable=True)
    mylist_storage = Column(String(128), nullable=True)
    mylist_source = Column(String(128), nullable=True)
    mylist_other = Column(String(128), nullable=True)
    lid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)

    updated = Column(DateTime(timezone=True), nullable=True)
    last_update_dice = Column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs):
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self):
        path = None
        if self.path:
            path = self.path.encode("utf-8")
        return (
            f"<FileTable(pk={self.pk}, path={path}, mylist_state={self.mylist_state}, "
            f"mylist_viewed={self.mylist_viewed}, updated={self.updated})>"
        )


class GroupTable(Base):
    __tablename__ = "group"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    gid = Column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    rating = Column(BigInteger().with_variant(Integer, "sqlite"))
    votes = Column(BigInteger().with_variant(Integer, "sqlite"))
    acount = Column(BigInteger().with_variant(Integer, "sqlite"))
    fcount = Column(BigInteger().with_variant(Integer, "sqlite"))
    name = Column(Unicode(248), nullable=False)
    short = Column(Unicode(64), nullable=False, index=True)
    irc_channel = Column(String(32))
    irc_server = Column(String(32))
    url = Column(String(248))
    picname = Column(String(32))
    founded = Column(DateTime(timezone=False))
    disbanded = Column(DateTime(timezone=False))
    dateflag = Column(Integer())
    last_release = Column(DateTime(timezone=False))
    last_activity = Column(DateTime(timezone=False))

    relations = relationship("GroupRelationTable", backref="group", cascade="all, delete")

    updated = Column(DateTime(timezone=True), nullable=True)
    last_update_dice = Column(DateTime(timezone=True), nullable=False)

    def update(self, **kwargs):
        for key, attr in kwargs.items():
            setattr(self, key, attr)

    def __repr__(self):
        return f"<GroupTable(pk={self.pk}, gid={self.gid}, name={self.name}>"


class GroupRelationTable(Base):
    __tablename__ = "group_relation"

    pk = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    group_pk = Column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("group.pk"), nullable=False)
    related_gid = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    relation_type: Column[str] = Column(
        Enum(GroupRelationType, values_callable=_values, name="group_relation_type_enum"),
        nullable=False,
    )

    # Identity equality, and no __eq__ -- see AnimeRelationTable above. The same
    # dead Python 2 __cmp__ used to sit here too.

    def __repr__(self):
        return (
            f"<GroupRelationTable(pk={self.pk}, group_pk={self.group_pk}, "
            f"related_gid={self.related_gid}, type={self.relation_type})>"
        )
