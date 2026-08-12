"""Tests that a cache session is always given back, however its block ends.

The object layer used to open a session and close it as two separate statements,
with the work in between. Anything raising in that work skipped the close, and
the surrounding `except` logged the database error rather than propagating it --
so the connection was leaked silently, on exactly the error paths that have the
least coverage.

These tests assert on the pool's checked-out count, because that is the thing
that was actually going wrong. Nothing here inspects `_db_session` for its own
sake: what matters is that after a failed cache read or write, the library is
holding no more connections than before it.
"""

import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

from tests import factories
from tests.objectlayer import FakeResponse


def _fail_with_the_connection_in_hand(session):
    """Fail the way a real database does: after the connection is checked out.

    A stub that raises before touching the connection would prove nothing -- the
    leak only exists once the pool has handed one over.
    """
    session.execute(sqlalchemy.text("select 1"))
    raise sqlalchemy.exc.OperationalError("select 1", None, Exception("database is locked"))


def _pool(anidb):
    return anidb._sessionmaker.kw["bind"].pool


class TestTheContextManagerItself:
    def test_a_block_that_raises_still_gives_the_connection_back(self, anidb):
        """The regression, stated directly. Nothing previously covered this."""
        anime = anidb.Anime(6187)
        pool = _pool(anidb)
        before = pool.checkedout()

        try:
            with anime._db_session() as sess:
                # Force a real checkout: a Session takes a connection lazily.
                sess.execute(sqlalchemy.text("select 1"))
                assert pool.checkedout() == before + 1
                raise RuntimeError("something in the middle went wrong")
        except RuntimeError:
            pass

        assert pool.checkedout() == before

    def test_a_block_that_returns_early_still_gives_the_connection_back(self, anidb):
        """Several of these blocks return from the middle; that skipped the close."""
        anime = anidb.Anime(6187)
        pool = _pool(anidb)
        before = pool.checkedout()

        def leaves_early():
            with anime._db_session() as sess:
                sess.execute(sqlalchemy.text("select 1"))
                return "early"

        assert leaves_early() == "early"
        assert pool.checkedout() == before


class TestErrorBranchesEndToEnd:
    """The paths that swallow a database error, exercised with one that happens."""

    def test_a_failed_mylist_lookup_answers_none_and_leaks_nothing(self, anidb, session, monkeypatch):
        """`Anime.in_mylist` logs the failure and answers None (SPEC-003).

        It is one of the branches whose `except` hid the leak: the query raised,
        the close never ran, and the caller saw only a None.
        """
        session.add(factories.make_anime(aid=6187))
        session.commit()
        anime = anidb.Anime(6187)
        pool = _pool(anidb)
        before = pool.checkedout()

        monkeypatch.setattr(
            sqlalchemy.orm.Query, "first", lambda self, *a, **kw: _fail_with_the_connection_in_hand(self.session)
        )

        assert anime.in_mylist is None
        assert pool.checkedout() == before

    def test_a_failed_cache_write_does_not_reach_the_caller(self, anidb, session, monkeypatch):
        """SPEC-003's best-effort rule: failing to cache must not fail the read.

        The reply is still applied to the in-memory object; only the write to the
        cache is lost, and the waiter is still released.
        """
        session.add(factories.make_anime(aid=6187))
        session.commit()
        anime = anidb.Anime(6187)
        pool = _pool(anidb)
        before = pool.checkedout()

        monkeypatch.setattr(
            sqlalchemy.orm.Session, "merge", lambda self, *a, **kw: _fail_with_the_connection_in_hand(self)
        )
        anime._db_data_callback(FakeResponse("230", datalines=[{"aid": "6187", "year": "2010"}]))

        assert anime._updated.is_set(), "the waiter must be released even when the cache write fails"
        assert pool.checkedout() == before
