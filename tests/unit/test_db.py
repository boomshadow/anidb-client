"""Tests for the cache schema.

The cache is what keeps this library off the network, so its schema is load
bearing: a column that silently fails to round-trip means a permanent cache miss
and a request to AniDB every single time.

Everything here runs against SQLite, in-memory or in tmp_path. No server, and
nothing to tear down.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from anidb_client.db import (
    AnimeRelationTable,
    AnimeTable,
    Base,
    EpisodeTable,
    FileTable,
    GroupRelationTable,
    GroupTable,
    init_db,
)

NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
    # Without this the pooled connections survive the test and are only closed
    # when the collector gets round to them.
    engine.dispose()


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
            assert s.query(AnimeTable).count() == 0

    def test_init_db_opens_in_memory_sqlite(self):
        """In-memory SQLite is the obvious choice for a caller's own test suite.

        It was also the one URL init_db could not open: pool_size/max_overflow
        were passed unconditionally, and in-memory SQLite gets a
        SingletonThreadPool, which accepts neither and raised TypeError.
        """
        factory = init_db("sqlite://")
        with factory() as s:
            assert s.query(AnimeTable).count() == 0


class TestRoundTrips:
    def test_anime_round_trips(self, session):
        session.add(_anime())
        session.commit()

        stored = session.query(AnimeTable).one()
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
        assert session.query(EpisodeTable).one().title_eng == "Erin and the Egg Thieves"

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
        assert session.query(EpisodeTable).one().title_kanji == "獣の奏者 エリン"

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

        stored = session.query(FileTable).one()
        assert stored.mylist_state == "on hdd"
        assert stored.size == 734003200

    def test_file_size_holds_values_beyond_32_bits(self, session):
        """A 4 GB+ release must not overflow: size is half of the AniDB file key."""
        big = 8 * 1024**3
        session.add(FileTable(aid=1, eid=1, is_generic=False, size=big, last_update_dice=NOW))
        session.commit()
        assert session.query(FileTable).one().size == big

    def test_group_round_trips(self, session):
        session.add(GroupTable(gid=1, name="Some Group", short="SG", last_update_dice=NOW))
        session.commit()
        assert session.query(GroupTable).one().short == "SG"


class TestEnumConstraints:
    def test_a_valid_mylist_state_is_accepted(self, session):
        session.add(FileTable(aid=1, eid=1, is_generic=False, mylist_state="deleted", last_update_dice=NOW))
        session.commit()
        assert session.query(FileTable).one().mylist_state == "deleted"

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
            session.query(FileTable).one()

    def test_a_valid_relation_type_is_accepted(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.query(AnimeTable).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="sequel"))
        session.commit()
        assert session.query(AnimeRelationTable).one().relation_type == "sequel"


class TestRelationships:
    def test_deleting_an_anime_cascades_to_its_relations(self, session):
        """Orphaned relation rows would resurrect as phantom entries on re-fetch."""
        session.add(_anime(aid=1))
        session.commit()
        anime = session.query(AnimeTable).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="sequel"))
        session.commit()
        assert session.query(AnimeRelationTable).count() == 1

        session.delete(anime)
        session.commit()
        assert session.query(AnimeRelationTable).count() == 0

    def test_anime_exposes_its_relations(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.query(AnimeTable).one()
        session.add(AnimeRelationTable(anime_pk=anime.pk, related_aid=2, relation_type="prequel"))
        session.commit()
        session.refresh(anime)
        assert [r.related_aid for r in anime.relations] == [2]


class TestUpdateHelper:
    def test_update_sets_the_given_attributes(self, session):
        session.add(_anime(aid=1))
        session.commit()
        anime = session.query(AnimeTable).one()

        anime.update(nr_of_episodes=51, year="2010")
        session.commit()

        stored = session.query(AnimeTable).one()
        assert (stored.nr_of_episodes, stored.year) == (51, "2010")


class TestRepr:
    """repr() is used in log messages on the cache-write error path.

    That is precisely when it must not itself raise, because it would replace a
    recoverable database warning with an unhandled exception.
    """

    def test_anime_repr(self, session):
        session.add(_anime(aid=6187))
        session.commit()
        assert "6187" in repr(session.query(AnimeTable).one())

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
