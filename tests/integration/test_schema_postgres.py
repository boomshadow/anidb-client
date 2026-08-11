"""Schema tests against a real PostgreSQL.

SQLite cannot answer these questions. Three things in this schema behave
differently on a server database, and each of them is a place a silent data bug
could live:

* `Enum(...)` becomes a native `CREATE TYPE ... AS ENUM` on PostgreSQL and a plain
  VARCHAR on SQLite. On PostgreSQL the database itself rejects an unknown value;
  on SQLite nothing does, and only SQLAlchemy's Python-side check catches it.
* `BigInteger().with_variant(Integer, "sqlite")` exists precisely *because* the
  two disagree. A SQLite-only suite always takes the variant branch and never
  checks that the real column is 64-bit.
* Foreign keys are enforced by default on PostgreSQL. SQLite ignores them unless
  `PRAGMA foreign_keys=ON`, so a cascade that "works" there may only be the ORM's
  doing.

Marked `postgres`; skipped when ANIDB_TEST_POSTGRES_URL is unset. CI asserts these
actually ran, so a silent skip cannot quietly weaken the pipeline.
"""

import datetime

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from anidb_client.db import AnimeRelationTable, AnimeTable, Base, FileTable, init_db
from tests import factories

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_engine(postgres_url):
    """A fresh schema for each test, torn down afterwards.

    drop_all matters more here than on SQLite: the native enum types are database
    objects in their own right and outlive their tables, so a partial teardown
    makes the next create_all fail with "type already exists".
    """
    engine = create_engine(postgres_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def pg_session(pg_engine):
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with factory() as s:
        yield s


class TestNativeEnums:
    def test_enum_columns_are_real_database_types(self, pg_engine):
        """On PostgreSQL these are CREATE TYPE enums, not strings."""
        with pg_engine.connect() as conn:
            names = {row[0] for row in conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))}
        assert {"mylist_state_enum", "anime_relation_type_enum", "episode_type_enum"} <= names

    def test_the_database_itself_rejects_an_unknown_enum_value(self, pg_engine):
        """The check SQLite cannot make.

        On SQLite the column is a VARCHAR and an unexpected value is stored
        happily, only failing later when SQLAlchemy converts it back on read. Here
        the write is refused outright, which is the stronger guarantee -- and the
        reason the enum lists are part of the protocol contract.
        """
        with pg_engine.connect() as conn, pytest.raises(sqlalchemy.exc.DBAPIError):
            conn.execute(
                text(
                    "INSERT INTO file (aid, eid, is_generic, mylist_state, last_update_dice) "
                    "VALUES (1, 1, false, 'nonsense', now())"
                )
            )

    def test_a_valid_enum_value_round_trips(self, pg_session):
        pg_session.add(
            FileTable(
                aid=1,
                eid=1,
                is_generic=False,
                mylist_state="on hdd",
                last_update_dice=datetime.datetime.now(datetime.UTC),
            )
        )
        pg_session.commit()
        assert pg_session.query(FileTable).one().mylist_state == "on hdd"


class TestBigIntegerColumns:
    def test_id_columns_are_64_bit_on_a_server_database(self, pg_engine):
        """`with_variant(Integer, "sqlite")` means SQLite never sees BIGINT.

        So this assertion is only meaningful here.
        """
        inspector = sqlalchemy.inspect(pg_engine)
        types = {c["name"]: str(c["type"]) for c in inspector.get_columns("file")}
        assert types["size"] == "BIGINT"
        assert types["fid"] == "BIGINT"

    def test_a_file_larger_than_32_bits_round_trips(self, pg_session):
        """A 4 GB+ release. Half of AniDB's file key is the size, so an overflow
        here is a permanent cache miss rather than a visible error."""
        big = 8 * 1024**3
        pg_session.add(
            FileTable(
                aid=1,
                eid=1,
                is_generic=False,
                size=big,
                last_update_dice=datetime.datetime.now(datetime.UTC),
            )
        )
        pg_session.commit()
        assert pg_session.query(FileTable).one().size == big


class TestConstraints:
    def test_foreign_keys_are_enforced(self, pg_session):
        """SQLite ignores foreign keys unless PRAGMA foreign_keys=ON.

        So the referential integrity this schema declares is only actually
        exercised against a server database.
        """
        pg_session.add(AnimeRelationTable(anime_pk=999999, related_aid=2, relation_type="sequel"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            pg_session.commit()

    def test_deleting_an_anime_cascades_to_its_relations(self, pg_session):
        pg_session.add(factories.make_anime(aid=1))
        pg_session.commit()
        anime = pg_session.query(AnimeTable).one()
        pg_session.add(factories.make_relation(anime.pk, related_aid=2))
        pg_session.commit()

        pg_session.delete(anime)
        pg_session.commit()
        assert pg_session.query(AnimeRelationTable).count() == 0

    def test_aid_is_unique(self, pg_session):
        pg_session.add(factories.make_anime(aid=1))
        pg_session.commit()
        pg_session.add(factories.make_anime(aid=1))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            pg_session.commit()


class TestInitDb:
    def test_init_db_opens_a_server_database(self, postgres_url, pg_engine):
        """init_db passes pool_size/max_overflow, which a QueuePool accepts.

        The SQLite in-memory case needed those arguments dropped; this confirms
        that the fix did not stop them being passed where they are meaningful.
        """
        factory = init_db(postgres_url)
        with factory() as sess:
            assert sess.query(AnimeTable).count() == 0
        bind = factory.kw["bind"]
        assert bind.pool.size() == 10
        bind.dispose()
