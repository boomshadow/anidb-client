"""HTTP error handling in the fanart.tv lookup.

fanart.tv rate-limits, and the code had a back-off for it that could never run:
it sat behind `if e.code != 404: ... return []`, a condition 429 satisfies. So a
rate-limited reply was indistinguishable from a hard failure, the Retry-After
header was never read, and the client kept its request rate exactly as it was.
"""

import urllib.error
import urllib.request

import pytest

import anidb_client
import anidb_client.animeobjs
from tests import factories

FANART_URL = "https://webservice.fanart.tv/v3.2/tv/83243"


class Raiser:
    """Stands in for urlopen, always failing the same way.

    conftest's network guard has already replaced urlopen with something that
    raises; this replaces the guard, so these tests still never reach the network.
    """

    def __init__(self, error):
        self._error = error
        self.calls = 0

    def __call__(self, req, *args, **kwargs):
        self.calls += 1
        raise self._error


def _http_error(code, headers=None):
    return urllib.error.HTTPError(url=FANART_URL, code=code, msg="nope", hdrs=headers or {}, fp=None)


@pytest.fixture
def an_anime(anidb, session, monkeypatch):
    session.add(factories.make_anime(aid=6187, type="TV Series"))
    session.commit()
    monkeypatch.setattr(anidb_client, "fanart_key", "a-key")
    return anidb.Anime(6187)


MOVIE_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<anime-list>
  <anime anidbid="7" tmdbid="1234" imdbid="tt0123456">
    <name>Kidou Senshi Gundam</name>
  </anime>
</anime-list>
"""


class Sequence:
    """Answers each call in turn: a payload to return, or an exception to raise."""

    def __init__(self, *steps):
        self._steps = list(steps)
        self._payload = b""
        self.calls = 0

    def __call__(self, req, *args, **kwargs):
        step = self._steps[self.calls]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        self._payload = step
        return self

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def a_movie(anidb, session, monkeypatch):
    """A movie mapped at two sources, so a failure on the second has something to
    discard if the code is wrong about what to return."""
    factories.install_anime_list(monkeypatch, MOVIE_LIST)
    session.add(factories.make_anime(aid=7, type="Movie"))
    session.commit()
    monkeypatch.setattr(anidb_client, "fanart_key", "a-key")
    return anidb.Anime(7)


@pytest.fixture
def slept(monkeypatch):
    """Record back-offs instead of taking them."""
    calls = []
    monkeypatch.setattr(anidb_client.animeobjs.time, "sleep", calls.append)
    return calls


class TestHttpErrors:
    def test_a_429_backs_off_for_the_time_the_server_asked_for(self, an_anime, slept, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", Raiser(_http_error(429, {"Retry-After": "7"})))

        assert an_anime.fanart == []
        assert slept == [7], "the Retry-After back-off is the branch that was unreachable"

    def test_a_429_without_a_retry_after_does_not_sleep_forever(self, an_anime, slept, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", Raiser(_http_error(429)))

        assert an_anime.fanart == []
        assert slept == [0]

    def test_a_404_is_simply_no_artwork(self, an_anime, slept, monkeypatch):
        """Not an error, and nothing to back off from."""
        monkeypatch.setattr(urllib.request, "urlopen", Raiser(_http_error(404)))

        assert an_anime.fanart == []
        assert slept == []

    def test_another_http_error_abandons_the_lookup(self, an_anime, slept, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", Raiser(_http_error(500)))

        assert an_anime.fanart == []
        assert slept == []

    def test_a_retry_after_longer_than_the_ceiling_is_truncated(self, an_anime, slept, monkeypatch):
        """Retry-After is chosen by the remote server. SPEC-002 explains at length
        why the UDP back-off has a ceiling; the same reasoning applies here."""
        monkeypatch.setattr(urllib.request, "urlopen", Raiser(_http_error(429, {"Retry-After": "999999"})))

        assert an_anime.fanart == []
        assert slept == [anidb_client.animeobjs.FANART_MAX_BACKOFF]

    def test_a_retry_after_that_is_a_date_is_not_a_crash(self, an_anime, slept, monkeypatch):
        """RFC 9110 allows an HTTP-date there as well as a delay in seconds."""
        monkeypatch.setattr(
            urllib.request, "urlopen", Raiser(_http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        )

        assert an_anime.fanart == []
        assert slept == [0]


class TestPartialResults:
    """SPEC-005: a transport failure ends the lookup and returns what was already
    gathered. Returning `[]` instead discarded a source that had answered."""

    def test_a_failure_on_a_later_id_keeps_what_earlier_ids_gave(self, a_movie, slept, monkeypatch):
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            Sequence(b'{"name": "A Movie"}', urllib.error.URLError("connection refused")),
        )

        assert a_movie.fanart == [{"name": "A Movie"}]

    def test_an_http_error_on_a_later_id_also_keeps_them(self, a_movie, slept, monkeypatch):
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            Sequence(b'{"name": "A Movie"}', _http_error(500)),
        )

        assert a_movie.fanart == [{"name": "A Movie"}]
