"""Tests for what happens when AniDB says it does not have something.

These paths were the least exercised in the library and the most dangerous when
wrong. The object layer waits on a `threading.Event` with no timeout, and only a
callback sets it -- so any callback that returns, or raises, before signalling
does not degrade, it deadlocks the calling application permanently.

Each test here asserts the completion event is set. That is the invariant: a
callback may fail, may mark the object illegal, may store nothing -- but it must
always signal that it is finished.
"""

import pytest

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
