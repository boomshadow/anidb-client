"""Tests for the cache schema.

The cache is what keeps this library off the network, so its schema is load
bearing: a column that silently fails to round-trip means a permanent cache miss
and a request to AniDB every single time.

Everything here runs against SQLite, in-memory or in tmp_path. No server, and
nothing to tear down.
"""

import datetime

import pytest
import sqlalchemy
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from anidb_client.db import (
    POOL_MAX_OVERFLOW,
    SQLITE_BUSY_TIMEOUT_SECONDS,
    AnimeRelationTable,
    AnimeTable,
    Base,
    EpisodeTable,
    FileTable,
    GroupRelationTable,
    GroupTable,
    MylistState,
    init_db,
    is_in_memory_sqlite,
)

NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def session(tmp_path):
    """A session on a cache opened the way the library opens one.

    Through init_db() rather than a bare create_engine, so everything below runs
    against the engine configuration real callers get -- foreign keys enforced,
    WAL where the filesystem grants it -- rather than against SQLite's defaults.
    """
    factory = init_db(f"sqlite:///{tmp_path}/cache.db")
    with factory() as s:
        yield s
    # Without this the pooled connections survive the test and are only closed
    # when the collector gets round to them.
    factory.kw["bind"].dispose()


def _anime(aid=6187, **kwargs):
    defaults = {
        "aid": aid,
        "year": "2009",
        "type": "TV Series",
        "nr_of_episodes": 50,
        "highest_episode_number": 50,
        "special_ep_count": 0,
        "vote_count": 0,
        "temp_vote_count": 0,
        "review_count": 0,
        "is_18_restricted": False,
        "anidb_updated": NOW.replace(tzinfo=None),
        "special_count": 0,
        "credit_count": 0,
        "other_count": 0,
        "trailer_count": 0,
        "parody_count": 0,
        "updated": NOW,
        "last_update_dice": NOW,
    }
    defaults.update(kwargs)
    return AnimeTable(**defaults)


class TestSchema:
    def test_init_db_creates_every_table(self, tmp_path):
        init_db(f"sqlite:///{tmp_path}/cache.db")
        expected = {
            "anime",
            "anime_relation",
            "episode",
            "file",
            "group",
            "group_relation",
        }
        assert expected <= set(Base.metadata.tables)

    def test_init_db_returns_a_usable_session_factory(self, tmp_path):
        factory = init_db(f"sqlite:///{tmp_path}/cache.db")
        with factory() as s:
            assert s.scalar(select(func.count()).select_from(AnimeTable)) == 0

    def test_init_db_opens_in_memory_sqlite(self):
        """In-memory SQLite is the obvious choice for a caller's own test suite.

        It was also the one URL init_db could not open: pool_size/max_overflow
        were passed unconditionally, and in-memory SQLite gets a
        SingletonThreadPool, which accepts neither and raised TypeError.
        """
        factory = init_db("sqlite://")
        with factory() as s:
            assert s.scalar(select(func.count()).select_from(AnimeTable)) == 0


class TestEngineConfiguration:
    """The engine is opened for a threaded library, not for SQLite's defaults.

    Those defaults are `journal_mode=delete` (one writer excludes every reader for
    the length of the busy timeout), foreign keys off, and -- as this library used
    to create it -- an unbounded connection pool.
    """

    def test_a_file_database_is_put_into_wal_mode(self, tmp_path):
        factory = init_db(f"sqlite:///{tmp_path}/cache.db")
        with factory() as s:
            assert s.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        factory.kw["bind"].dispose()

    def test_a_refused_journal_mode_still_yields_a_working_cache(self, tmp_path, monkeypatch):
        """WAL is asked for, not required.

        It does not work over a network filesystem, and this package supports
        `nfs://` paths. Simulated by making the statement fail rather than by
        needing a network share: what matters is that the cache still opens.
        """
        real = sqlalchemy.engine.Connection.exec_driver_sql

        def refuse(self, statement, *args, **kwargs):
            if "journal_mode" in statement:
                raise OperationalError(statement, None, Exception("disk I/O error"))
            return real(self, statement, *args, **kwargs)

        monkeypatch.setattr(sqlalchemy.engine.Connection, "exec_driver_sql", refuse)

        factory = init_db(f"sqlite:///{tmp_path}/cache.db")
        with factory() as s:
            assert s.scalar(select(func.count()).select_from(AnimeTable)) == 0
        factory.kw["bind"].dispose()

    def test_an_in_memory_cache_keeps_the_mode_it_can_have(self):
        """The other half of the fallback, and a real one: `:memory:` has no WAL."""
        factory = init_db("sqlite://")
        with factory() as s:
            assert s.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "memory"
            assert s.scalar(select(func.count()).select_from(AnimeTable)) == 0

    def test_foreign_keys_are_enforced_by_the_database(self, session):
        """Not merely declared. SQLite ignores them unless each connection opts in."""
        assert session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

        session.add(AnimeRelationTable(anime_pk=404, related_aid=2, relation_type="sequel"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_the_busy_timeout_is_the_one_we_chose(self, session):
        """Set through the driver's own connect argument, read back as the pragma."""
        expected_ms = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
        assert session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar() == expected_ms

    def test_the_pool_is_bounded(self, tmp_path):
        """It was max_overflow=-1: unlimited, by SQLAlchemy's own documentation."""
        factory = init_db(f"sqlite:///{tmp_path}/cache.db")
        pool = factory.kw["bind"].pool
        assert pool.size() == 10
        assert pool._max_overflow == POOL_MAX_OVERFLOW
        factory.kw["bind"].dispose()

    def test_the_pool_size_can_be_overridden(self, tmp_path):
        factory = init_db(f"sqlite:///{tmp_path}/cache.db", pool_size=3)
        assert factory.kw["bind"].pool.size() == 3
        factory.kw["bind"].dispose()


class TestInMemoryDetection:
    """Used by init() to refuse a cache that cannot serve a threaded client."""

    @pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "sqlite:///file:c?mode=memory&uri=true"])
    def test_an_in_memory_url_is_recognised(self, url):
        assert is_in_memory_sqlite(url)

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///anidb.db",
            "sqlite:////var/tmp/anidb.db",
            "postgresql://user:pass@dbhost/anidb",
            "not a url at all",
        ],
    )
    def test_everything_else_is_not(self, url):
        assert not is_in_memory_sqlite(url)


class TestRoundTrips:
    def test_anime_round_trips(self, session):
        session.add(_anime())
        session.commit()

        stored = session.scalars(select(AnimeTable)).one()
        assert stored.aid == 6187
        assert stored.nr_of_episodes == 50
        assert stored.type == "TV Series"

    def test_aid_is_unique(self, session):
        session.add(_anime(aid=1))
        session.commit()
        session.add(_anime(aid=1))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_episode_round_trips(self, session):
        session.add(
            EpisodeTable(
                aid=6187,
                eid=96461,
                length=25,
                votes=0,
                epno="5",
                title_eng="Erin and the Egg Thieves",
                type="regular",
                updated=NOW,
                last_update_dice=NOW,
            )
        )
        session.commit()
        assert session.scalars(select(EpisodeTable)).one().title_eng == "Erin and the Egg Thieves"

    def test_episode_kanji_title_survives_a_round_trip(self, session):
        """Titles are Unicode columns; AniDB returns kanji for most series."""
        session.add(
            EpisodeTable(
                aid=6187,
                eid=1,
                length=25,
                votes=0,
                epno="1",
                title_kanji="獣の奏者 エリン",
                type="regular",
                updated=NOW,
                last_update_dice=NOW,
            )
        )
        session.commit()
        assert session.scalars(select(EpisodeTable)).one().title_kanji == "獣の奏者 エリン"

    def test_file_round_trips_with_its_mylist_state(self, session):
        session.add(
            FileTable(
                aid=6187,
                eid=96461,
                fid=12345,
                is_generic=False,
                ed2khash="d41d8cd98f00b204e9800998ecf8427e",
                size=734003200,
                mylist_state="on hdd",
                last_update_dice=NOW,
            )
        )
        session.commit()

        stored = session.scalars(select(FileTable)).one()
        assert stored.mylist_state == "on hdd"
        assert stored.size == 734003200

    def test_file_size_holds_values_beyond_32_bits(self, session):
        """A 4 GB+ release must not overflow: size is half of the AniDB file key."""
        big = 8 * 1024**3
        session.add(FileTable(aid=1, eid=1, is_generic=False, size=big, last_update_dice=NOW))
        session.commit()
        assert session.scalars(select(FileTable)).one().size == big

    def test_group_round_trips(self, session):
        session.add(GroupTable(gid=1, name="Some Group", short="SG", last_update_dice=NOW))
        session.commit()
        assert session.scalars(select(GroupTable)).one().short == "SG"


class TestEnumConstraints:
    def test_a_valid_mylist_state_is_accepted(self, session):
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state="deleted", last_update_dice=NOW))
        session.commit()
        assert session.scalars(select(FileTable)).one().mylist_state == "deleted"

    def test_an_out_of_range_enum_value_writes_but_cannot_be_read_back(self, session):
        """The enum is checked on the way out of the database, not on the way in.

        Since SQLAlchemy 1.4, Enum defaults to `create_constraint=False`, so no
        CHECK constraint is emitted and the column is a plain VARCHAR on disk.
        Writing an unexpected value therefore succeeds; it is only rejected when
        a query converts it back, which raises LookupError.

        The consequence is worth being explicit about: if AniDB ever returns a
        mylist state this schema does not list, the row is written happily and
        then poisons every subsequent read of it. That is a worse failure than
        rejecting the write would have been, and it is why the enum lists are
        part of the protocol contract rather than documentation.
        """
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state="nonsense", last_update_dice=NOW))
        session.commit()

        session.expunge_all()
        with pytest.raises(LookupError, match="not among the defined enum values"):
            session.scalars(select(FileTable)).one()

    def test_a_valid_relation_type_is_accepted(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.scalars(select(AnimeTable)).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="sequel"))
        session.commit()
        assert session.scalars(select(AnimeRelationTable)).one().relation_type == "sequel"


class TestTheVocabularyIsStoredByValue:
    """The columns hold AniDB's wording, not the enum member names.

    SQLAlchemy's default for a Python enum is to persist `member.name`, which
    would put ON_HDD on disk and in PostgreSQL's `CREATE TYPE`. `values_callable`
    in db.py overrides that. Nothing else in the suite would notice if it were
    dropped -- a StrEnum member compares equal to its value either way, so every
    assertion elsewhere would still pass while the stored vocabulary silently
    changed and every existing cache stopped reading back.
    """

    def test_the_column_holds_anidbs_wording(self, session):
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state=MylistState.ON_HDD, last_update_dice=NOW))
        session.commit()

        # Straight past the ORM: this is what is actually on disk.
        stored = session.connection().exec_driver_sql("select mylist_state from file").scalar()

        assert stored == "on hdd"

    def test_a_member_and_its_string_are_interchangeable_on_the_way_in(self, session):
        """Callers hold plain strings; the schema must keep accepting them."""
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state="on hdd", last_update_dice=NOW))
        session.add(FileTable(aid=2, eid=2, is_generic=False, mylist_state=MylistState.ON_HDD, last_update_dice=NOW))
        session.commit()

        stored = session.scalars(select(FileTable).order_by(FileTable.aid))
        assert [f.mylist_state for f in stored] == ["on hdd", "on hdd"]

    def test_a_value_read_back_is_a_member(self, session):
        """Which is what lets the converters hand members straight to the schema."""
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state="deleted", last_update_dice=NOW))
        session.commit()
        session.expunge_all()

        assert session.scalars(select(FileTable)).one().mylist_state is MylistState.DELETED


class TestRelationships:
    def test_deleting_an_anime_cascades_to_its_relations(self, session):
        """Orphaned relation rows would resurrect as phantom entries on re-fetch."""
        session.add(_anime(aid=1))
        session.commit()
        anime = session.scalars(select(AnimeTable)).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="sequel"))
        session.commit()
        assert session.scalar(select(func.count()).select_from(AnimeRelationTable)) == 1

        session.delete(anime)
        session.commit()
        assert session.scalar(select(func.count()).select_from(AnimeRelationTable)) == 0

    def test_anime_exposes_its_relations(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.scalars(select(AnimeTable)).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="prequel"))
        session.commit()
        session.refresh(anime)
        assert [r.related_aid for r in anime.relations] == [2]


class TestUpdateHelper:
    """`update(**fields)` is how a refresh applies a whole reply at once.

    It exists for the one thing direct assignment cannot do: take a dictionary of
    field names AniDB decided and set them all. It used to be reached for on paths
    that had no such dictionary either -- assigning a timestamp to a column was an
    error to a type checker under the old declarative style, so the helper was the
    way past it. It is not needed for that any more, and those call sites now assign
    directly, which leaves this with only its real purpose.

    Four copies of it, one per row class, so each is exercised: the copies are
    identical today and nothing would notice if one drifted.
    """

    def test_update_sets_the_given_attributes(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.scalars(select(AnimeTable)).one()

        anime.update(nr_of_episodes=51, year="2010")
        session.commit()

        stored = session.scalars(select(AnimeTable)).one()
        assert (stored.nr_of_episodes, stored.year) == (51, "2010")

    def test_update_sets_the_given_attributes_on_an_episode(self, session):
        session.add(
            EpisodeTable(aid=1, eid=2, length=25, votes=0, epno="1", type="regular", updated=NOW, last_update_dice=NOW)
        )
        session.commit()
        episode = session.scalars(select(EpisodeTable)).one()

        episode.update(length=24, title_eng="A Title")
        session.commit()

        stored = session.scalars(select(EpisodeTable)).one()
        assert (stored.length, stored.title_eng) == (24, "A Title")

    def test_update_sets_the_given_attributes_on_a_file(self, session):
        session.add(FileTable(aid=1, eid=1, is_generic=False, last_update_dice=NOW))
        session.commit()
        file = session.scalars(select(FileTable)).one()

        file.update(mylist_state="on hdd", mylist_viewed=True)
        session.commit()

        stored = session.scalars(select(FileTable)).one()
        assert (stored.mylist_state, stored.mylist_viewed) == ("on hdd", True)

    def test_update_sets_the_given_attributes_on_a_group(self, session):
        session.add(GroupTable(gid=1, name="Some Group", short="SG", last_update_dice=NOW))
        session.commit()
        group = session.scalars(select(GroupTable)).one()

        group.update(name="Renamed Group", votes=7)
        session.commit()

        stored = session.scalars(select(GroupTable)).one()
        assert (stored.name, stored.votes) == ("Renamed Group", 7)


class TestRepr:
    """repr() is used in log messages on the cache-write error path.

    That is precisely when it must not itself raise, because it would replace a
    recoverable database warning with an unhandled exception.
    """

    def test_anime_repr(self, session):
        session.add(_anime(aid=6187))
        session.commit()
        assert "6187" in repr(session.scalars(select(AnimeTable)).one())

    def test_episode_repr(self, session):
        ep = EpisodeTable(aid=1, eid=2, length=25, votes=0, epno="1", type="regular", updated=NOW, last_update_dice=NOW)
        session.add(ep)
        session.commit()
        assert "epno=1" in repr(ep)

    def test_file_repr_encodes_a_non_ascii_path(self, session):
        f = FileTable(aid=1, eid=1, is_generic=False, path="/媒体/anime.mkv", last_update_dice=NOW)
        session.add(f)
        session.commit()
        assert repr(f)

    def test_group_repr(self, session):
        g = GroupTable(gid=1, name="Some Group", short="SG", last_update_dice=NOW)
        session.add(g)
        session.commit()
        assert "Some Group" in repr(g)

    def test_anime_relation_repr(self, session):
        r = AnimeRelationTable(anime_pk=1, related_aid=2, relation_type="sequel")
        assert "sequel" in repr(r)

    def test_group_relation_repr(self, session):
        r = GroupRelationTable(group_pk=1, related_gid=2, relation_type="merged from")
        assert "merged from" in repr(r)
