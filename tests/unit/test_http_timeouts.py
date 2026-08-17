"""Every HTTP fetch the library makes must be bounded by a timeout.

urllib defaults to no timeout at all. A server that accepts the connection and
then stops talking blocks the calling thread for as long as it cares to, and none
of these calls are made from a thread anyone supervises -- the titles fetch
happens on the caller's own thread, inside what looks like an ordinary attribute
read. That is a hang with no UDP involved, so none of the transport's own
timeouts cover it.

These assert the argument is passed rather than that a stall is survived:
reproducing a half-open server here would test CPython's socket module rather
than this library.
"""

import pathlib
import tempfile
import urllib.request

import pytest

import anidb_client
import anidb_client.anames
from tests import factories


class RecordingOpen:
    """Stands in for urlopen, capturing how it was called."""

    def __init__(self, payload=b""):
        self.calls = []
        self._payload = payload

    def __call__(self, req, *args, **kwargs):
        self.calls.append({"url": getattr(req, "full_url", req), "args": args, "kwargs": kwargs})
        return self

    # Enough of the response surface for the callers below.
    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def timeouts(self):
        """The timeout each call passed, positionally or by keyword."""
        return [c["kwargs"].get("timeout", c["args"][0] if c["args"] else None) for c in self.calls]


@pytest.fixture
def urlopen(monkeypatch):
    """Replace urlopen for the duration of a test.

    conftest's network guard has already replaced it with something that raises;
    this replaces the guard, which is why these tests still never reach the
    network.
    """
    recorder = RecordingOpen()
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


def test_the_configured_timeout_is_a_positive_number():
    assert isinstance(anidb_client.HTTP_TIMEOUT, int | float)
    assert anidb_client.HTTP_TIMEOUT > 0


def test_no_urlopen_call_site_is_left_unbounded():
    """A structural check over the package source.

    The tests below cover the call sites that exist today. This one is what
    notices a new fetch being added without a timeout, which they cannot.
    """
    package = pathlib.Path(anidb_client.__file__).parent
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in package.glob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "urlopen(" in line and "timeout" not in line and not line.lstrip().startswith("#")
    ]

    assert not offenders, "urlopen without a timeout blocks forever:\n" + "\n".join(offenders)


class TestTitleCacheFetch:
    """The fetch that matters most, because it is the least visible.

    `Anime("some title")` fetches this XML whenever the cached copy is older than
    36 hours, so an unbounded fetch here hangs what the caller thinks is a lookup.
    """

    def test_the_xml_fetch_passes_a_timeout(self, urlopen, tmp_path, monkeypatch):
        # Nothing installs a logger here. This test used to, which is why it never
        # saw that the fetch it covers logged through a global that was None until
        # init() -- see tests/unit/test_xml_cache_fetch.py.
        #
        # Take the non-posix branch of the cache-path choice so the download lands
        # in tmp_path instead of /var/tmp. Everything else here is the real thing:
        # a real temp file is written, renamed, and left in the test's own tmp_path.
        monkeypatch.setattr(anidb_client.anames.os, "name", "nt")
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        # The recorded payload is not gzipped XML, and verification is not what is
        # under test here.
        monkeypatch.setattr(anidb_client.anames, "_verify_xml_file", lambda _path: True)

        anidb_client.anames.update_xml("https://anidb.net/api/anime-titles.xml.gz")

        assert urlopen.timeouts == [anidb_client.HTTP_TIMEOUT]


class TestImageAndFanartFetches:
    def test_download_image_passes_a_timeout(self, urlopen, anidb, session):
        # A real Anime rather than a stub: download_image rejects anything whose
        # type is not exactly Anime or Group, so a subclass will not do.
        session.add(factories.make_anime(aid=6187, picname="12345.jpg"))
        session.commit()

        class Sink:
            def write(self, data):
                pass

        anidb_client.download_image(Sink(), anidb.Anime(6187))

        assert urlopen.timeouts == [anidb_client.HTTP_TIMEOUT]

    def test_download_fanart_passes_a_timeout(self, urlopen, monkeypatch):
        monkeypatch.setattr(anidb_client, "fanart_key", "key")

        class Sink:
            def write(self, data):
                pass

        anidb_client.download_fanart(Sink(), "http://assets.fanart.tv/fanart/x.png")

        assert urlopen.timeouts == [anidb_client.HTTP_TIMEOUT]
