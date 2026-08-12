"""Tests for what happens when AniDB says it does not have something.

These paths were the least exercised in the library and the most dangerous when
wrong. A callback that returns, or raises, before signalling does not degrade the
result -- it strands whoever asked for it.

Most tests here assert the completion event is set. That is the invariant: a
callback may fail, may mark the object illegal, may store nothing -- but it must
always signal that it is finished.

`TestNoReplyAtAll` covers the other half, and the one the reported incident took:
no callback runs at all, because there is no reply. Nothing sets the event
because nothing was ever going to. That case cannot be fixed by being careful
inside callbacks; it needs the request itself to carry a failure, and the wait on
it to be bounded.
"""

import pytest

from anidb_client.errors import AniDBBannedError, AniDBCommandTimeoutError
from tests import factories
from tests.objectlayer import FakeResponse


class TestAnimeNotFound:
    def test_a_330_reply_signals_completion_instead_of_raising(self, anidb):
        """A 330 carries no data lines, and two separate faults met here.

        `res.datalines[0]` was read before the rescode was checked, raising
        IndexError on every 330; and the handler then logged through `self.log`,
        an attribute that does not exist, after having already set
        `_illegal_object` -- so __getattribute__ raised before the event was set.
        Either one left the waiter blocked forever.
        """
        anime = anidb.Anime(6187)
        anime._updated.clear()

        anime._db_data_callback(FakeResponse("330", datalines=[]))

        assert anime._updated.is_set(), "a 330 reply must signal completion"
        assert anime._illegal_object is True

    def test_asking_for_an_unknown_anime_raises_rather_than_hanging(self, anidb, link):
        """End to end: the failure this protects against is a hang, not an error.

        pytest-timeout is the backstop -- if this regresses it fails on time
        rather than passing.
        """
        link.on("ANIME", FakeResponse("330", datalines=[]))
        anime = anidb.Anime(6187)

        with pytest.raises(anidb.errors.IllegalAnimeObject):
            _ = anime.year

    def test_a_normal_reply_still_stores_its_data(self, anidb, link):
        """The reordering must not have broken the path that does return data."""
        link.on("ANIME", FakeResponse("230", datalines=[{"aid": "6187", "year": "2009", "type": "TV Series"}]))
        anime = anidb.Anime(6187)

        assert anime.year == "2009"
        assert anime.type == "TV Series"


class TestFileNotFound:
    @pytest.mark.parametrize("code", ["320", "340"])
    def test_an_unidentifiable_file_signals_completion(self, anidb, link, code):
        """An fid AniDB does not have, with no local path to guess from.

        Three separate faults met on this path, each of which hung the caller:

        * `_guess_anime_ep_from_file` read `self.path`, which on a File with no
          local path falls through __getattr__ to update_if_old() -- starting a
          fetch that waits on the very event this callback exists to set.
        * With nothing to guess from it returns (None, None), and `anime.aid`
          then raised AttributeError, which the except clause did not catch.
        * The handler marked the file illegal and then called
          `self._file_updated.set()` -- but that attribute was not in
          __getattribute__'s allowlist, so reading it on a now-illegal object
          raised too.
        """
        link.on("FILE", FakeResponse(code, datalines=[]))
        f = anidb.File(fid=99999)
        f._file_updated.clear()

        f._anidb_file_data_callback(FakeResponse(code, datalines=[]))

        assert f._file_updated.is_set(), f"a {code} reply must signal completion"

    def test_a_known_file_still_parses_its_reply(self, anidb, session, link):
        """The happy path must survive the not-found fixes."""
        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96461))
        session.add(factories.make_file(aid=6187, eid=96461, fid=12345))
        session.commit()

        f = anidb.File(fid=12345)
        f._file_updated.clear()
        # `state` is always present in a real reply -- it is part of the fmask the
        # client sends -- and the handler reads it unconditionally.
        f._anidb_file_data_callback(
            FakeResponse("220", datalines=[{"fid": "12345", "aid": "6187", "eid": "96461", "state": "1"}])
        )

        assert f._file_updated.is_set()


class TestCallbacksAlwaysSignal:
    def test_every_completion_event_is_set_on_every_return_path(self):
        """A static check over the callbacks, as a guard against the whole class.

        Two of these bugs were the same shape -- a return that skipped the event --
        and reviewing for it by eye is exactly what failed the first time. This
        walks the AST instead: for each callback that signals completion at all,
        no `return` may appear before the first signal.
        """
        import ast
        import pathlib

        import anidb_client.animeobjs

        events = {"_updated", "_file_updated", "_mylist_updated"}
        tree = ast.parse(pathlib.Path(anidb_client.animeobjs.__file__).read_text())

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or "callback" not in node.name:
                continue
            set_lines = sorted(
                n.lineno
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "set"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr in events
            )
            if not set_lines:
                continue
            for ret in (n.lineno for n in ast.walk(node) if isinstance(n, ast.Return)):
                if not any(s <= ret for s in set_lines):
                    offenders.append(f"{node.name}: return at line {ret} precedes any completion signal")

        assert offenders == [], "; ".join(offenders)


class TestNoReplyAtAll:
    """The incident, from the caller's side.

    AniDB answered `555 BANNED`, so no callback ever ran, so nothing set the
    event the object layer was waiting on. `Anime(5587)` never returned. The
    process stayed alive and idle, wrote nothing, produced no exit code, and had
    to be killed -- and to the caller that was indistinguishable from work still
    in progress.

    Each of these gives the transport's answer as "there will not be one", which
    is the case a recording double that only ever invokes callbacks cannot reach.
    """

    def test_a_banned_request_raises_instead_of_hanging(self, anidb, link):
        link.fails("ANIME", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError):
            _ = anidb.Anime(6187).year

    def test_a_timed_out_request_raises_instead_of_hanging(self, anidb, link):
        link.fails("ANIME", AniDBCommandTimeoutError("ANIME went unanswered"))

        with pytest.raises(AniDBCommandTimeoutError):
            _ = anidb.Anime(6187).year

    def test_the_reason_reaches_the_caller_unchanged(self, anidb, link):
        """Not just "something went wrong": which thing, so a caller can act.

        A ban means back off and try later; a not-found means stop asking. The
        object layer used to be able to express neither -- an unknown attribute
        answers None, and a ban answered nothing at all.
        """
        link.fails("EPISODE", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError, match="555 BANNED"):
            _ = anidb.Episode(eid=96461).title

    def test_a_failed_request_does_not_leave_the_object_locked(self, anidb, link):
        """The lock is taken by update() and released by the thread doing the work.

        Released in a finally, or the first failure leaves the object marked as
        permanently updating -- after which every later update() takes the "one is
        already running" branch and returns without doing anything, and the object
        never refreshes again for the life of the process.
        """
        link.fails("ANIME", AniDBBannedError("555 BANNED"))
        anime = anidb.Anime(6187)

        with pytest.raises(AniDBBannedError):
            anime.update(block=True)

        assert anime._updating.acquire(False), "the update lock was never released"
        anime._updating.release()

    def test_a_request_nobody_is_waiting_on_does_not_raise(self, anidb, link):
        """A non-blocking update has no caller to raise to.

        It still fails, and the transport still logs it; what it must not do is
        take down the thread of whoever happened to trigger it.
        """
        link.fails("ANIME", AniDBBannedError("555 BANNED"))

        anidb.Anime(6187).update(block=False)
