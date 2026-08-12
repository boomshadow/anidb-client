"""Render the cache schema as DDL, and hold it against a stored snapshot.

The cache has no migration story (SPEC-003): the documented upgrade is to delete
the database and let it repopulate. That makes an accidental schema change
survivable, and it is exactly why one must not be able to happen quietly -- every
user pays for it with a rebuild and a fresh run at AniDB's rate limit. A
deliberate schema change should be its own visible commit with its own reasoning,
not something that rode along inside a refactor.

So the schema is snapshotted rather than described. The models are compiled to the
DDL a backend would actually be sent, and the result is compared byte for byte
with a file in `schema_snapshots/`. Anything the compiler emits differently -- a
column, a type, a nullability, an index, a constraint, an enum label or its
position -- fails the build.

Two dialects, because they disagree on purpose: `BigInteger().with_variant(Integer,
"sqlite")` exists precisely so the two render differently, and the constrained
vocabularies become native `CREATE TYPE` enums on PostgreSQL and plain VARCHARs on
SQLite. A snapshot of one backend would not notice a change confined to the other.

Regenerating is deliberate and reviewable: `task schema-snapshot` rewrites both
files, and the diff is the change being proposed.
"""

import difflib
import sys
from pathlib import Path

import sqlalchemy
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import CreateEnumType
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from anidb_client.db import Base

SNAPSHOT_DIR = Path(__file__).parent / "schema_snapshots"

# The dialects a snapshot is kept for, by the name of the file holding it. MySQL is
# supported (SPEC-003) but neither pinned here nor tested in CI; adding it would mean
# a third snapshot nobody exercises against a real server.
DIALECTS: dict[str, Dialect] = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),
}


def _statement(element, dialect: Dialect) -> str:
    """One compiled DDL statement, normalised to something a text file can hold.

    SQLAlchemy indents with a tab and leaves a trailing space after each column,
    neither of which survives an editor honouring `.editorconfig`. Stripping both
    cannot hide a schema change -- no DDL difference is expressible in trailing
    whitespace -- and it means a snapshot that round-trips through an editor still
    matches.
    """
    rendered = str(element.compile(dialect=dialect)).strip()
    lines = [line.replace("\t", "    ").rstrip() for line in rendered.splitlines()]
    return "\n".join(lines) + ";"


def _enum_types(dialect: Dialect) -> list[str]:
    """`CREATE TYPE ... AS ENUM (...)` for every constrained vocabulary.

    Only PostgreSQL has these as objects in their own right; on SQLite the same
    columns are VARCHARs whose length is the longest member. They are rendered
    separately because `CreateTable` refers to the type by name and never defines
    it, so without this the snapshot would pin which vocabulary a column uses but
    not what is in it -- and the labels are the part with a live cache depending on
    them. `values_callable` in db.py is what keeps them AniDB's wording rather than
    the enum member names, and this is where that becomes visible.
    """
    if dialect.name != "postgresql":
        return []
    types: dict[str, sqlalchemy.Enum] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, sqlalchemy.Enum):
                types[str(column.type.name)] = column.type
    return [_statement(CreateEnumType(types[name]), dialect) for name in sorted(types)]


def render_schema(dialect: Dialect) -> str:
    """Every table, index and enum type in the cache schema, in a stable order.

    `sorted_tables` is topological and then alphabetical; indexes come off a set and
    are sorted by name. Neither depends on the order the models happen to be
    declared in, so moving a class in db.py is not a snapshot change.
    """
    statements = _enum_types(dialect)
    for table in Base.metadata.sorted_tables:
        statements.append(_statement(CreateTable(table), dialect))
        statements.extend(
            _statement(CreateIndex(index), dialect) for index in sorted(table.indexes, key=lambda i: str(i.name))
        )
    return "\n\n".join(statements) + "\n"


def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.sql"


def read_snapshot(name: str) -> str:
    return snapshot_path(name).read_text(encoding="utf-8")


def diff_against_snapshot(name: str, actual: str) -> str:
    """A unified diff, or the empty string when they match.

    Returned rather than asserted on so the caller decides what to say; a bare
    equality assertion on two hundred lines of DDL is unreadable in a pytest report.
    """
    expected = read_snapshot(name)
    if expected == actual:
        return ""
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"snapshot: {snapshot_path(name).name}",
            tofile="rendered from db.py",
        )
    )


def write_snapshots() -> None:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    for name, dialect in DIALECTS.items():
        snapshot_path(name).write_text(render_schema(dialect), encoding="utf-8")
        print(f"wrote {snapshot_path(name)}")


if __name__ == "__main__":
    write_snapshots()
    sys.exit(0)
