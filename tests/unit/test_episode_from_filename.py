"""Tests for turning a matched filename into AniDB episode numbers.

`_search_filename` takes a matched regex and produces AniDB episode identifiers:
a bare number for a regular episode, `S<n>` for a special, `C<n>` for credits
(openings and endings), `T<n>` for a trailer. Getting this wrong does not raise --
it files an episode under the wrong number, which then goes into mylist wrong.

The rest of the filename-inference surface is covered separately; this module is
scoped to the conversion step, which is where the crash was.
"""

import pytest

import anidb_client.fileinfo as fileinfo
from tests import factories


@pytest.fixture
def searcher(anidb, session):
    """A real File, used to reach _search_filename.

    The method itself uses no instance state, but AniDBObj's __getattribute__ and
    __getattr__ overrides mean a half-built object recurses -- so it has to be a
    properly constructed one.
    """
    session.add(factories.make_anime(aid=6187, credit_count=4))
    session.add(factories.make_episode(aid=6187, eid=96461))
    session.add(factories.make_file(aid=6187, eid=96461, fid=12345))
    session.commit()
    return anidb.File(fid=12345)


@pytest.fixture
def anime(anidb, searcher):
    """The anime `searcher` already seeded -- inserting it twice violates aid's
    unique constraint, which is itself worth knowing the schema enforces."""
    return anidb.Anime(6187)


def _first_match(filename):
    """The first pattern that matches, mirroring how the guesser iterates."""
    for regex in fileinfo.ep_nr_re:
        if regex is not None and regex.search(filename):
            return regex
    raise AssertionError(f"no pattern matched {filename!r}")


def search(searcher, anime, filename):
    return searcher._search_filename(filename, _first_match(filename), anime)


class TestRegularEpisodes:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("/show/foo.s01.e05.mkv", ["5"]),
            ("/show/foo.S01E05.mkv", ["5"]),
            ("/show/foo.ep05.mkv", ["5"]),
            ("/show/foo.EP_05.mkv", ["5"]),
            ("/show/foo.1x09.mkv", ["9"]),
            ("/show/foo - 05.mkv", ["5"]),
        ],
    )
    def test_an_episode_number_becomes_a_bare_number(self, searcher, anime, filename, expected):
        """Leading zeros are dropped: AniDB numbers episodes 1, not 01."""
        assert search(searcher, anime, filename) == expected


class TestSpecials:
    def test_a_numbered_special_keeps_its_number(self, searcher, anime):
        assert search(searcher, anime, "/show/foo.special.02.mkv") == ["S2"]

    def test_an_unnumbered_special_is_the_first_of_its_kind(self, searcher, anime):
        """Regression: this raised UnboundLocalError.

        The specials pattern allows the number to be absent, so group(2) is an
        empty string and int("") raises ValueError. The handler for that then read
        `ep` -- unbound, because int() had just failed -- instead of the captured
        text. It crashed rather than falling back.

        Worse, it was unreachable until recently: the fallback loop used to retry
        only the final catch-all pattern, so the empty-capture patterns never ran.
        Fixing that loop is what exposed this.
        """
        assert search(searcher, anime, "/show/foo.special.mkv") == ["S1"]

    def test_an_unnumbered_opening_is_the_first_of_its_kind(self, searcher, anime):
        assert search(searcher, anime, "/show/foo.NCOP.mkv") == ["C1"]

    def test_a_numbered_opening_becomes_a_credit(self, searcher, anime):
        assert search(searcher, anime, "/show/foo.NCOP01.mkv") == ["C1"]

    def test_a_trailer_becomes_a_trailer_number(self, searcher, anime):
        assert search(searcher, anime, "/show/foo.trailer01.mkv") == ["T1"]


class TestEndings:
    def test_an_ending_is_offset_past_the_openings(self, anidb, session, searcher):
        """AniDB files openings and endings in one 'credits' sequence.

        The library guesses that endings start halfway through it, so with
        credit_count=4 the first ending is credit 3. A guess, but a documented
        one -- and pinned here so it cannot drift silently.
        """
        session.add(factories.make_anime(aid=1, credit_count=4))
        session.commit()
        anime = anidb.Anime(1)

        assert search(searcher, anime, "/show/foo.NCED01.mkv") == ["C3"]

    def test_an_ending_is_not_offset_when_the_credit_count_is_unknown(self, anidb, session, searcher):
        session.add(factories.make_anime(aid=1, credit_count=0))
        session.commit()
        anime = anidb.Anime(1)

        assert search(searcher, anime, "/show/foo.NCED01.mkv") == ["C1"]


class TestMultipleEpisodes:
    def test_a_range_yields_every_episode_in_it(self, searcher, anime):
        """A file covering several episodes has to report all of them.

        This is what feeds `multiep`, and what mylist add/remove iterate over.
        """
        assert search(searcher, anime, "/show/foo.ep01-03.mkv") == ["1", "3"]


class TestRomanNumerals:
    def test_a_roman_numeral_is_converted(self, searcher, anime, monkeypatch):
        """The numeral branch read the wrong variable and could never run.

        None of the episode patterns capture letters, so this is unreachable
        through them today -- it is exercised directly to prove the branch works
        rather than raising, since a future pattern could reach it.
        """
        import re

        numeral = re.compile(r"()(iv)()")
        assert searcher._search_filename("iv", numeral, anime) == ["4"]

    def test_unconvertible_text_is_skipped_with_a_warning(self, searcher, anime, caplog):
        import re

        garbage = re.compile(r"()(zz)()")
        with caplog.at_level("WARNING", logger="anidb_client.test"):
            assert searcher._search_filename("zz", garbage, anime) == []
        assert "non-numeric episode number" in caplog.text


class TestNoMatch:
    def test_a_pattern_that_does_not_match_yields_nothing(self, searcher, anime):
        import re

        assert searcher._search_filename("/show/foo.mkv", re.compile(r"(x)(y)(z)"), anime) == []
