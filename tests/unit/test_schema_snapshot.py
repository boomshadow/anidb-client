"""The SQLite half of the schema snapshot.

Why a snapshot at all is argued in `tests/schema_snapshot.py`; the short version is
that this cache has no migration story, so an unintended schema change costs every
user a rebuild and there is nothing downstream that would catch it. This turns
"did we change the schema" from a review question into a build failure.

SQLite is checked here because it needs no server and is what almost every caller
actually runs. The PostgreSQL half is in `tests/integration/test_schema_postgres.py`
behind the `postgres` marker, where the native enum types and the wide-integer
variant it alone renders can be held against a real server as well.

When one of these fails and the change was intended: `task schema-snapshot`, then
read the diff before committing it. The diff is the schema change.
"""

from anidb_client.db import Base
from tests import schema_snapshot

EXPECTED_TABLES = {
    "anime",
    "anime_relation",
    "episode",
    "file",
    "group",
    "group_relation",
}


class TestSqliteSchemaSnapshot:
    def test_the_rendered_ddl_matches_the_snapshot(self):
        """Byte for byte, against `tests/schema_snapshots/sqlite.sql`."""
        actual = schema_snapshot.render_schema(schema_snapshot.DIALECTS["sqlite"])
        difference = schema_snapshot.diff_against_snapshot("sqlite", actual)
        assert not difference, (
            "The SQLite schema no longer matches its snapshot.\n\n"
            f"{difference}\n"
            "If the change was intended, run `task schema-snapshot` and commit the "
            "result -- and say in the commit message why the schema changed, since "
            "the cache has no migration story and every user rebuilds."
        )

    def test_every_model_is_in_the_snapshot(self):
        """Named here rather than derived, so a deleted model is not a silent pass.

        Everything else in this file compares one rendering of `Base.metadata`
        against another, so a model that disappeared would drop out of both and
        regenerating would make the mismatch go away. This list is the independent
        statement of what the cache is supposed to hold.
        """
        assert set(Base.metadata.tables) == EXPECTED_TABLES
        snapshot = schema_snapshot.read_snapshot("sqlite")
        for table in EXPECTED_TABLES:
            assert f"CREATE TABLE {table} (" in snapshot or f'CREATE TABLE "{table}" (' in snapshot
