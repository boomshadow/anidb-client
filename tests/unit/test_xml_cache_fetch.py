"""Fetching the two bulk XML documents: where from, what a failure answers with, and what it leaves behind.

These cover `update_xml` and the two refresh entry points the package exports,
around the fetch rather than through it -- urlopen is replaced, so nothing here
reaches the network.

The theme is that none of it may depend on `init()` having run. `update_anilist()`
and `update_animetitles()` are public, they are pure local-file-and-HTTP work, and a
caller reaching for a mapping before opening a UDP session is using them as
intended. They logged through a global that `init()` filled in, so a log call on
that path raised `AttributeError` over whatever it was reporting on -- on the
success path too, before the document was ever written. So no test in here installs
a logger: that is the point of them.
"""

import logging
import os
import tempfile
import time
import urllib.error
import urllib.request

import pytest

import anidb_client
import anidb_client.anames
from anidb_client.errors import AniDBFileError

# Enough of a document to parse. Verification -- which counts entries -- is stubbed
# in the tests that are not about it.
MINIMAL_XML = b"<anime-titles><anime aid='1'><title>x</title></anime></anime-titles>"


class Fetched:
    """Stands in for urlopen with a payload, or with a failure."""

    def __init__(self, payload=MINIMAL_XML, error=None):
        self._payload = payload
        self._error = error

    def __call__(self, req, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Land the download in tmp_path instead of the real /var/tmp.

    Takes the non-posix branch of update_xml's cache-path choice. Everything else
    is the real thing: a real temporary file is written and renamed.
    """
    monkeypatch.setattr(anidb_client.anames.os, "name", "nt")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def fetch(monkeypatch, payload=MINIMAL_XML, error=None):
    """Install a stand-in urlopen, replacing conftest's network guard."""
    monkeypatch.setattr(urllib.request, "urlopen", Fetched(payload, error))


def partials(cache_dir):
    """Temporary files update_xml left behind, by the name only it uses."""
    return sorted(p.name for p in cache_dir.iterdir() if p.name.startswith(".anidb_client_cache"))


def test_the_library_has_a_logger_before_init_runs():
    """The invariant the rest of this module rests on.

    `log` used to be None until `init()` assigned it, which made every logging call
    in the package a latent AttributeError on any path reachable earlier.
    """
    assert isinstance(anidb_client.log, logging.Logger)


class TestTheConfiguredSources:
    """Structural, because the alternative is a test that fetches.

    A test that the URL resolves would need the network, which this suite refuses
    for good reason (an accidental fetch of the titles export gets whoever runs the
    suite next banned for 24 hours) -- and it would report someone else's outage as
    a bug in this package, which is exactly how the URL below came to be doubted.
    """

    def test_the_titles_export_comes_from_anidb(self):
        assert anidb_client.anames._animetitles_url.startswith("https://anidb.net/")

    def test_the_mapping_document_comes_from_the_canonical_raw_host(self):
        """Not github.com's /raw/ redirect: one hop, and a different serving path."""
        assert anidb_client.anames._anime_list_url.startswith(
            "https://raw.githubusercontent.com/Anime-Lists/anime-lists/"
        )

    def test_both_sources_are_fetched_over_https(self):
        for url in (anidb_client.anames._animetitles_url, anidb_client.anames._anime_list_url):
            assert url.startswith("https://")


class TestFetchingBeforeInit:
    """No logger installed. Every one of these used to raise AttributeError."""

    def test_a_successful_fetch_is_written_and_returned(self, cache_dir, monkeypatch):
        """The success path logs before it writes, so it failed first and hardest."""
        fetch(monkeypatch)
        monkeypatch.setattr(anidb_client.anames, "_verify_xml_file", lambda _path: True)

        result = anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert result == str(cache_dir / "anime-list.xml")
        assert (cache_dir / "anime-list.xml").read_bytes() == MINIMAL_XML

    def test_a_successful_fetch_leaves_no_partial_file(self, cache_dir, monkeypatch):
        fetch(monkeypatch)
        monkeypatch.setattr(anidb_client.anames, "_verify_xml_file", lambda _path: True)

        anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert partials(cache_dir) == []

    def test_a_failed_fetch_answers_none_rather_than_raising(self, cache_dir, monkeypatch):
        """A 404 is the shape that started this: the handler raised over the error."""
        fetch(monkeypatch, error=urllib.error.HTTPError("http://x", 404, "Not Found", {}, None))

        assert anidb_client.anames.update_xml("https://example.invalid/anime-list.xml") is None

    def test_a_failed_fetch_is_reported(self, cache_dir, monkeypatch, caplog):
        """Degrading silently would leave the caller with no way to find out why."""
        fetch(monkeypatch, error=OSError("connection reset"))

        with caplog.at_level(logging.ERROR, logger="anidb_client"):
            anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert "connection reset" in caplog.text

    def test_a_failed_fetch_leaves_no_partial_file(self, cache_dir, monkeypatch):
        fetch(monkeypatch, error=OSError("connection reset"))

        anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert partials(cache_dir) == []

    def test_a_failed_fetch_falls_back_to_the_copy_on_disk(self, cache_dir, monkeypatch):
        """SPEC-003: being unable to refresh is not a reason to lose what is held."""
        stale = cache_dir / "anime-list.xml"
        stale.write_bytes(MINIMAL_XML)
        os.utime(stale, (0, 0))
        fetch(monkeypatch, error=OSError("connection reset"))

        assert anidb_client.anames.update_xml("https://example.invalid/anime-list.xml") == str(stale)

    def test_a_download_that_fails_verification_leaves_no_partial_file(self, cache_dir, monkeypatch):
        """A truncated download still parses, so this path is reachable in earnest."""
        fetch(monkeypatch)
        monkeypatch.setattr(anidb_client.anames, "_verify_xml_file", lambda _path: False)

        result = anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert result is None
        assert partials(cache_dir) == []

    def test_a_cleanup_that_cannot_remove_the_file_does_not_raise(self, cache_dir, monkeypatch):
        """Cleanup failing must not replace the failure it is cleaning up after."""
        fetch(monkeypatch, error=OSError("connection reset"))
        real_remove = os.remove

        def refuse(path):
            if ".anidb_client_cache" in str(path):
                raise OSError("read-only file system")
            real_remove(path)

        monkeypatch.setattr(anidb_client.anames.os, "remove", refuse)

        assert anidb_client.anames.update_xml("https://example.invalid/anime-list.xml") is None


class TestARefreshThatFailed:
    """What a failed refresh answers with, and how loudly it says so.

    The two ways a refresh can fail used to be handled differently: a server that
    could not be reached fell back to the copy on disk, while a document that
    arrived and did not verify reported total failure -- with the good copy still
    sitting there, untouched, one line away. Detecting a bad download left the
    caller worse off than never having tried. They are one situation now.
    """

    @pytest.fixture
    def cached_copy(self, cache_dir):
        """A cached document old enough that a refresh is attempted, 40h back."""
        copy = cache_dir / "anime-list.xml"
        copy.write_bytes(MINIMAL_XML)
        stamp = time.time() - 40 * 3600
        os.utime(copy, (stamp, stamp))
        return copy

    @pytest.fixture
    def corrupt_download(self, monkeypatch):
        """A download that arrives and does not verify -- truncated, or an error page."""
        fetch(monkeypatch)
        monkeypatch.setattr(anidb_client.anames, "_verify_xml_file", lambda _path: False)

    def test_a_corrupt_download_falls_back_to_the_copy_on_disk(self, corrupt_download, cached_copy):
        """The fix: a bad download is a failed fetch, not a worse outcome than one."""
        result = anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert result == str(cached_copy)

    def test_falling_back_does_not_overwrite_the_good_copy(self, corrupt_download, cached_copy):
        """The bad bytes are discarded; what stays in use already passed the check."""
        anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert cached_copy.read_bytes() == MINIMAL_XML

    def test_a_corrupt_download_with_nothing_to_fall_back_on_still_answers_none(self, corrupt_download, cache_dir):
        """A first fetch has no last-known-good, so this one does reach the caller."""
        assert anidb_client.anames.update_xml("https://example.invalid/anime-list.xml") is None

    def test_falling_back_warns_rather_than_errors(self, corrupt_download, cached_copy, caplog):
        """The level states the outcome: the caller is about to get an answer."""
        with caplog.at_level(logging.DEBUG, logger="anidb_client"):
            anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert [r.levelname for r in caplog.records if r.levelname == "ERROR"] == []
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_falling_back_names_how_stale_the_answer_has_become(self, corrupt_download, cached_copy, caplog):
        """Nothing bounds staleness, so the age is the only place drift is visible."""
        with caplog.at_level(logging.WARNING, logger="anidb_client"):
            anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert "40h old" in caplog.text

    def test_having_nothing_to_fall_back_on_errors(self, corrupt_download, cache_dir, caplog):
        """The caller is about to be told, so this one is not survivable."""
        with caplog.at_level(logging.DEBUG, logger="anidb_client"):
            anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_an_unreachable_server_warns_when_it_falls_back(self, cached_copy, monkeypatch, caplog):
        """The fallback that already existed reports at the level of its outcome too.

        It survives the failure and answers the caller, so it warns; it does not
        get to log an error about something nothing noticed.
        """
        fetch(monkeypatch, error=OSError("connection reset"))

        with caplog.at_level(logging.DEBUG, logger="anidb_client"):
            result = anidb_client.anames.update_xml("https://example.invalid/anime-list.xml")

        assert result == str(cached_copy)
        assert [r.levelname for r in caplog.records if r.levelname == "ERROR"] == []
        assert "connection reset" in caplog.text


class TestARefreshThatCannotProduceADocument:
    """The two public refresh entry points, when the fetch answers with nothing.

    `update_animetitles()` read the document into its table unconditionally, so a
    refresh that produced nothing assigned None over a table that was already
    loaded -- and then returned as though it had worked. The caller found out one
    call later, from a vaguer error, having lost the table it had.
    """

    @pytest.fixture
    def no_document(self, monkeypatch):
        monkeypatch.setattr(anidb_client.anames, "update_xml", lambda _url: None)

    def test_refreshing_the_titles_raises(self, no_document, monkeypatch):
        monkeypatch.setattr(anidb_client.anames, "titles", None, raising=False)

        with pytest.raises(AniDBFileError, match="list of anime titles"):
            anidb_client.anames.update_animetitles()

    def test_refreshing_the_titles_raises_even_with_a_table_already_loaded(self, no_document, monkeypatch):
        """The case that used to return quietly, having emptied the table."""
        monkeypatch.setattr(anidb_client.anames, "titles", object(), raising=False)

        with pytest.raises(AniDBFileError, match="list of anime titles"):
            anidb_client.anames.update_animetitles()

    def test_refreshing_the_mappings_raises(self, no_document, monkeypatch):
        monkeypatch.setattr(anidb_client.anames, "anilist", None, raising=False)

        with pytest.raises(AniDBFileError, match="list of anime mappings"):
            anidb_client.anames.update_anilist()

    def test_refreshing_the_mappings_raises_even_with_a_table_already_loaded(self, no_document, monkeypatch):
        monkeypatch.setattr(anidb_client.anames, "anilist", {"1": {}}, raising=False)

        with pytest.raises(AniDBFileError, match="list of anime mappings"):
            anidb_client.anames.update_anilist()

    def test_a_refresh_that_fell_back_to_disk_still_loads_the_table(self, cache_dir, monkeypatch):
        """The failure above is only for having no usable document at all."""
        stale = cache_dir / "anime-titles.xml"
        stale.write_bytes(MINIMAL_XML)
        monkeypatch.setattr(anidb_client.anames, "update_xml", lambda _url: str(stale))
        monkeypatch.setattr(anidb_client.anames, "titles", None, raising=False)

        anidb_client.anames.update_animetitles()

        assert anidb_client.anames.titles is not None
