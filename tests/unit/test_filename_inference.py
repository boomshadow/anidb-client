"""Tests for identifying an anime and episode from a file on disk.

This is what the library does when AniDB has never seen a file: guess. It reads
the parent directory, falls back to the filename, strips release-group brackets
and codec tags, and fuzzy-matches what is left against the anime-titles list.

It is the most user-visible logic here -- it decides what a file *is* -- and it
is heuristic, so the tests are about pinning the heuristics rather than proving
them optimal. Two of them changed recently (the fallback loop that never ran, and
an `re.sub` given a flag where it takes a count), and nothing was watching.
"""

import pytest

from tests import factories


@pytest.fixture
def guesser(anidb, session, tmp_path):
    """A File over a real path, so the guessing runs the way it does in anger."""
    session.add(factories.make_anime(aid=6187, nr_of_episodes=50, credit_count=4))
    session.add(factories.make_anime(aid=1, nr_of_episodes=13))
    session.commit()
    return anidb


def _episode_reply(cmd):
    """Answer an EPISODE lookup by echoing back what was asked for.

    Identification does not finish at the guess: the guessed Episode is then
    resolved against AniDB to get its eid. Without a reply here the episode comes
    back "no such episode" and the file is marked unidentifiable -- so the tests
    would be measuring the stub, not the guessing.
    """
    from tests.objectlayer import FakeResponse

    return FakeResponse(
        "240",
        datalines=[
            {
                "eid": "900001",
                "aid": str(cmd.parameters.get("aid") or 6187),
                "epno": str(cmd.parameters.get("epno") or "1"),
                "length": "25",
                "votes": "0",
                "type": "1",
            }
        ],
    )


def make_file(anidb, tmp_path, relative, link, **kwargs):
    """Create the file on disk and hand back a File for it.

    AniDB is told it has never seen the file, which is what puts the library on
    the guessing path.
    """
    from tests.objectlayer import FakeResponse

    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really a video")
    link.on("FILE", FakeResponse("320", datalines=[]))
    link.on("EPISODE", _episode_reply)
    return anidb.File(path=str(path), **kwargs)


class TestAnimeFromDirectory:
    def test_the_parent_directory_names_the_anime(self, guesser, tmp_path, link):
        """The directory is tried first: it is usually the series title, cleanly."""
        f = make_file(guesser, tmp_path, "Kemono no Souja Erin/whatever - 05.mkv", link)
        assert f.anime.aid == 6187

    def test_an_english_title_also_matches(self, guesser, tmp_path, link):
        f = make_file(guesser, tmp_path, "Crest of the Stars/Crest of the Stars - 03.mkv", link)
        assert f.anime.aid == 1


class TestAnimeFromFilename:
    def test_release_group_brackets_are_stripped_before_matching(self, guesser, tmp_path, link):
        """`[Group]` and `(1080p)` are noise that would wreck a fuzzy match."""
        f = make_file(guesser, tmp_path, "unsorted/[SomeGroup] Kemono no Souja Erin - 05 (1080p).mkv", link)
        assert f.anime.aid == 6187

    def test_a_lowercase_episode_marker_is_stripped(self, guesser, tmp_path, link):
        """Regression: the strip used re.sub(..., re.IGNORECASE) positionally.

        re.sub's fourth positional parameter is `count`, so that passed 2 as a
        replacement limit and never applied the flag -- leaving lowercase "ep05"
        in the string being matched against the title list.
        """
        f = make_file(guesser, tmp_path, "unsorted/Kemono no Souja Erin ep05.mkv", link)
        assert f.anime.aid == 6187

    def test_a_name_matching_nothing_leaves_the_file_unidentified(self, guesser, tmp_path, link):
        """And says so rather than picking the least-bad match.

        A confident wrong answer here files someone's episode under another
        series entirely.
        """
        f = make_file(guesser, tmp_path, "unsorted/qqqq zzzz wwww - 01.mkv", link)
        with pytest.raises(guesser.errors.IllegalAnimeObject):
            _ = f.anime


class TestEpisodeNumber:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Kemono no Souja Erin - 05.mkv", "5"),
            ("Kemono no Souja Erin ep05.mkv", "5"),
            ("Kemono no Souja Erin - S01E05.mkv", "5"),
            ("Kemono no Souja Erin 1x05.mkv", "5"),
        ],
    )
    def test_the_episode_number_is_read_from_the_filename(self, guesser, tmp_path, link, filename, expected):
        f = make_file(guesser, tmp_path, f"Kemono no Souja Erin/{filename}", link)
        assert f.episode.episode_number == expected

    def test_an_unnumbered_special_is_identified(self, guesser, tmp_path, link):
        """Regression: this raised UnboundLocalError.

        It was unreachable until the fallback loop was fixed to actually try the
        patterns after the first confident ones -- and the first thing the newly
        reachable patterns did was crash.
        """
        f = make_file(guesser, tmp_path, "Kemono no Souja Erin/Kemono no Souja Erin special.mkv", link)
        assert f.episode.episode_number == "S1"

    def test_a_credit_is_identified_as_a_credit(self, guesser, tmp_path, link):
        """Openings are AniDB credits, not episode 1 of the series."""
        f = make_file(guesser, tmp_path, "Kemono no Souja Erin/Kemono no Souja Erin NCOP01.mkv", link)
        assert f.episode.episode_number.startswith("C")


class TestSingleEpisodeSeries:
    def test_a_one_episode_anime_assumes_episode_one(self, anidb, session, tmp_path, link):
        """A movie's filename usually carries no episode number at all."""
        session.add(factories.make_anime(aid=1, nr_of_episodes=1))
        session.commit()

        f = make_file(anidb, tmp_path, "Crest of the Stars/Crest of the Stars.mkv", link)
        assert f.episode.episode_number == "1"

    def test_force_single_episode_series_overrides_the_guess(self, guesser, tmp_path, link):
        """The caller can assert it is a movie when the numbering says otherwise.

        Without the flag a 50-episode series with no number in the filename is
        simply unidentifiable; with it, the file is episode 1.
        """
        f = make_file(
            guesser,
            tmp_path,
            "Kemono no Souja Erin/Kemono no Souja Erin.mkv",
            link,
            force_single_episode_series=True,
        )
        assert f.episode.episode_number == "1"


class TestParseDir:
    def test_the_directory_normally_wins_over_the_filename(self, guesser, tmp_path, link):
        """The parent directory is consulted first and its match is taken.

        So a file sitting in the wrong directory is filed by the directory, which
        is the behaviour parse_dir=False exists to switch off.
        """
        f = make_file(guesser, tmp_path, "Crest of the Stars/Kemono no Souja Erin - 05.mkv", link)
        assert f.anime.aid == 1

    def test_parse_dir_false_makes_the_filename_decide(self, guesser, tmp_path, link):
        """For a flat dump directory the parent name is meaningless or misleading."""
        f = make_file(
            guesser,
            tmp_path,
            "Crest of the Stars/Kemono no Souja Erin - 05.mkv",
            link,
            parse_dir=False,
        )
        assert f.anime.aid == 6187


class TestHeuristicLimits:
    def test_an_episode_marker_with_no_separator_before_it_is_not_matched(self, guesser, tmp_path, link):
        """ "ep03.mkv" is not recognised: the pattern requires a separator first.

        Recorded as a known limit rather than fixed. Loosening it would change
        which files are identified in existing collections, which is a decision
        rather than a tidy-up -- the same reasoning as the "pt2" part-file case.
        """
        f = make_file(guesser, tmp_path, "Kemono no Souja Erin/ep03.mkv", link)
        with pytest.raises(guesser.errors.IllegalAnimeObject):
            _ = f.episode
