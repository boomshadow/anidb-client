"""Tests for how init() sources AniDB credentials.

The rule: credentials are needed only to open the UDP session. A db_only client
never opens one and must not be asked for them; anything else must have them,
from arguments or from a netrc file.

That condition was wrong -- it read `if not (user and pass) or db_only`, so
db_only *forced* the netrc lookup. These tests pin all four combinations, since
the fix moved a boolean and the failure mode is a library that refuses to start.
"""

import logging

import pytest

import anidb_client


@pytest.fixture
def clean_globals(monkeypatch):
    """init() writes module globals; restore them regardless of what happens."""
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)


@pytest.fixture
def netrc_file(tmp_path):
    path = tmp_path / "netrc"
    path.write_text("machine api.anidb.net\n  login netrcuser\n  password netrcpass\n  account netrckey\n")
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def captured_link(monkeypatch):
    """Replace AniDBLink so a non-db_only init() can run without a socket.

    This patches the collaborator, not the code under test: init() still does all
    of its own credential resolution, and what it resolved is then inspectable.
    """
    calls = []

    class StubLink:
        def __init__(self, user, pwd, **kwargs):
            calls.append({"user": user, "pwd": pwd, **kwargs})

    monkeypatch.setattr(anidb_client.link, "AniDBLink", StubLink)
    return calls


class TestDbOnly:
    def test_db_only_needs_no_credentials_and_no_netrc(self, tmp_path, clean_globals):
        """The regression. db_only exists for cache-only use, and demanded a netrc."""
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True, netrc_file=str(tmp_path / "absent"))

        with anidb_client.get_session() as sess:
            assert sess is not None
        anidb_client._sessionmaker.kw["bind"].dispose()

    def test_db_only_opens_no_udp_link(self, tmp_path, clean_globals, captured_link):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", db_only=True)

        assert captured_link == [], "db_only must not construct a link"
        anidb_client._sessionmaker.kw["bind"].dispose()


class TestCredentialsRequired:
    def test_missing_credentials_without_a_netrc_is_an_error(self, tmp_path, clean_globals):
        """And a typed one -- it used to raise a bare Exception."""
        with pytest.raises(anidb_client.errors.AniDBError, match="username and password"):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=str(tmp_path / "absent"))

    def test_a_netrc_naming_no_anidb_host_is_the_same_error(self, tmp_path, clean_globals, captured_link):
        """A file that exists but has nothing for AniDB in it.

        The absent-file case above is caught before the lookup runs. This one
        gets all the way through it and comes out with the credentials still
        unset -- which used to open the link anyway, so the failure surfaced at
        AUTH with nothing pointing back at the netrc file that did not have what
        was wanted.
        """
        path = tmp_path / "netrc"
        path.write_text("machine example.com\n  login someone\n  password something\n")
        path.chmod(0o600)

        with pytest.raises(anidb_client.errors.AniDBError, match="username and password"):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=str(path))

        assert captured_link == [], "no link may be opened without credentials"

    def test_explicit_credentials_are_passed_through(self, tmp_path, clean_globals, captured_link):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p")

        assert captured_link[0]["user"] == "u"
        assert captured_link[0]["pwd"] == "p"
        anidb_client._sessionmaker.kw["bind"].dispose()


class TestNetrcLookup:
    def test_credentials_are_read_from_netrc_when_not_given(self, tmp_path, clean_globals, captured_link, netrc_file):
        """The path the review bot asked about: the lookup must still happen.

        Moving `db_only` out of that condition could plausibly have disabled it
        for everyone rather than only for db_only clients.
        """
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=netrc_file)

        assert captured_link[0]["user"] == "netrcuser"
        assert captured_link[0]["pwd"] == "netrcpass"
        anidb_client._sessionmaker.kw["bind"].dispose()

    def test_the_netrc_account_field_supplies_the_encryption_key(
        self, tmp_path, clean_globals, captured_link, netrc_file
    ):
        """`account` is where the API key lives, per the README."""
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=netrc_file)

        assert captured_link[0]["api_key"] == "netrckey"
        anidb_client._sessionmaker.kw["bind"].dispose()

    def test_an_explicit_api_key_beats_the_netrc_one(self, tmp_path, clean_globals, captured_link, netrc_file):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=netrc_file, api_key="explicit")

        assert captured_link[0]["api_key"] == "explicit"
        anidb_client._sessionmaker.kw["bind"].dispose()

    def test_explicit_credentials_are_not_overridden_by_netrc(self, tmp_path, clean_globals, captured_link, netrc_file):
        anidb_client.init(f"sqlite:///{tmp_path}/cache.db", netrc_file=netrc_file, api_user="u", api_pass="p")

        assert captured_link[0]["user"] == "u"
        assert captured_link[0]["pwd"] == "p"
        anidb_client._sessionmaker.kw["bind"].dispose()
