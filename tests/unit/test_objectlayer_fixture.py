"""Tests for the object-layer fixture itself.

Everything in the object-layer suites rests on this: real Anime/Episode/File
objects, taking their ordinary code paths, backed by a seeded cache and never
touching the network. If the fixture silently started reaching AniDB, or silently
stopped exercising the real classes, the suites above it would keep passing while
testing nothing. So the fixture gets its own tests.
"""

import pytest

from tests import factories


def test_init_opens_a_cache_without_credentials(anidb):
    """db_only must not demand a username, password or netrc file.

    It used to: the condition read `if not (user and pass) or db_only`, so passing
    db_only *forced* the netrc lookup and raised when none existed -- refusing to
    start for precisely the cache-only use it exists for.
    """
    with anidb.get_session() as sess:
        assert sess is not None


def test_the_library_is_wired_to_the_recording_link(anidb, link):
    assert anidb._anidb is link
    assert link.requests == []


def test_anime_is_built_from_the_cache_without_any_request(anidb, session, link):
    session.add(factories.make_anime(aid=6187))
    session.commit()

    anime = anidb.Anime(6187)

    assert anime.aid == 6187
    assert anime.nr_of_episodes == 50
    assert anime.type == "TV Series"
    assert link.requests == [], "a cached anime must not cause an API request"


def test_anime_resolves_a_title_through_the_real_matcher(anidb, session, link):
    """Titles come from the supplied XML via get_titles(), not from a stub."""
    session.add(factories.make_anime(aid=6187))
    session.commit()

    assert anidb.Anime("Kemono no Souja Erin").aid == 6187
    assert anidb.Anime("Crest of the Stars").aid == 1
    assert link.requests == []


def test_constructing_an_anime_does_not_by_itself_fetch(anidb, link):
    """Construction resolves the title and reads cache; it sends nothing.

    The fetch is lazy -- it happens on first access of a field that has to come
    from AniDB. That is what makes `Anime(aid)` cheap, and it is why the tests
    below assert on attribute access rather than on construction.
    """
    anidb.Anime(6187)
    assert link.requests == []


def test_reading_an_uncached_field_does_hit_the_api(anidb, link):
    """The fixture does not suppress requests -- it records them.

    Worth pinning: a test that forgets to seed its row exercises the fetch path,
    not the cache path, and should be able to tell the difference.
    """
    from tests.objectlayer import FakeResponse

    link.on("ANIME", FakeResponse("230", datalines=[{"aid": "6187", "year": "2009"}]))
    anime = anidb.Anime(6187)

    assert anime.year == "2009"
    assert link.commands() == ["ANIME"]


def test_an_unscripted_request_gets_a_not_found_reply_rather_than_hanging(anidb, link):
    """The recording link always answers, and this is why.

    The object layer waits on a threading.Event with no timeout, and only the
    callback sets it -- so a double that stays silent does not produce an empty
    result, it deadlocks the test. Unscripted commands get their documented
    "not found" code instead.
    """
    anime = anidb.Anime(6187)

    with pytest.raises(anidb.errors.IllegalAnimeObject):
        _ = anime.year
    assert link.commands() == ["ANIME"]


def test_file_is_built_from_the_cache_without_any_request(anidb, session, link):
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    session.add(factories.make_file(aid=6187, eid=96461, fid=12345))
    session.commit()

    f = anidb.File(fid=12345)

    assert f.fid == 12345
    assert f.anime.aid == 6187
    assert f.episode.episode_number == "5"
    assert link.requests == []


def test_the_network_guard_still_applies_under_this_fixture(anidb):
    """The autouse guard is not disabled by initialising the library."""
    import socket

    from tests.conftest import ExternalNetworkBlocked

    with pytest.raises(ExternalNetworkBlocked):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("api.anidb.net", 9000))


def test_globals_are_restored_between_tests(anidb):
    """Guards against cross-test leakage through the library's module globals.

    Paired with the test below: this one sets a value, that one asserts it is gone.
    Ordering is not relied upon -- both assert the same invariant from a clean
    fixture, and monkeypatch is what actually restores it.
    """
    assert anidb._sessionmaker is not None
    anidb.fanart_key = "leaked-from-a-test"


def test_no_state_leaks_from_the_previous_test(anidb):
    assert anidb.fanart_key is None
