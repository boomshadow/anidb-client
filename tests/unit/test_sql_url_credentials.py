"""Tests for looking the cache database's password up in netrc.

If the sql-url carries no password, init() fills one in from netrc. That was
string surgery on `sql_db_url.split("/")`, and it got the common case wrong in
both directions:

- it tested `":" not in parts[2]` to mean "no password given", but a netloc also
  contains a colon before a *port* -- so every URL naming a port, which is most
  of them, skipped the lookup entirely and connected with no password;
- it interpolated the credentials into the string raw. A password containing any
  of :/?#@ then produced a URL SQLAlchemy does not merely misread but rejects:
  `make_url("postgresql://u:p@ss:w/rd#1@host:5432/db")` raises ValueError out of
  init() itself.

These assert through `make_url`, because SQLAlchemy is what actually consumes
this string, and it is what percent-decodes the values back out.
"""

import logging

import pytest
from sqlalchemy.engine.url import make_url

import anidb_client


@pytest.fixture
def clean_globals(monkeypatch):
    for name, value in (
        ("log", logging.getLogger("anidb_client.test")),
        ("_anidb", None),
        ("_sessionmaker", None),
        ("fanart_key", None),
    ):
        monkeypatch.setattr(anidb_client, name, value, raising=False)


@pytest.fixture
def captured_url(monkeypatch):
    """Capture the URL init() hands to the database layer, and open nothing."""
    seen = []

    def fake_init_db(url, **kwargs):
        seen.append(url)
        return object()

    monkeypatch.setattr(anidb_client.db, "init_db", fake_init_db)
    return seen


def write_netrc(tmp_path, machine="dbhost", login="dbuser", password="s3cret"):
    path = tmp_path / "netrc"
    # login=None writes an entry with no login line at all, which netrc permits and
    # which reads back as an empty username.
    login_line = f"  login {login}\n" if login is not None else ""
    path.write_text(f"machine {machine}\n{login_line}  password {password}\n")
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def run_init(tmp_path, clean_globals, captured_url):
    """Run init() for its URL rewriting alone, and parse the result as SQLAlchemy will."""

    def go(sql_url, **netrc_kwargs):
        anidb_client.init(sql_url, db_only=True, netrc_file=write_netrc(tmp_path, **netrc_kwargs))
        return make_url(captured_url[0])

    return go


class TestTheRegression:
    def test_a_url_with_a_port_still_gets_its_password(self, run_init):
        """The bug. A colon meant "has a password", and a port is a colon."""
        url = run_init("postgresql://dbuser@dbhost:5432/anidb_cache")

        assert url.password == "s3cret"
        assert url.port == 5432, "the port must survive the rewrite"
        assert url.host == "dbhost"
        assert url.database == "anidb_cache"

    def test_a_url_without_a_port_gets_its_password(self, run_init):
        """The one shape that already worked. Pinned so the fix keeps it."""
        url = run_init("postgresql://dbuser@dbhost/anidb_cache")

        assert url.password == "s3cret"
        assert url.port is None

    def test_a_url_with_no_username_takes_the_one_from_netrc(self, run_init):
        url = run_init("postgresql://dbhost:5432/anidb_cache")

        assert url.username == "dbuser"
        assert url.password == "s3cret"


class TestQuoting:
    def test_a_password_containing_url_syntax_arrives_intact(self, run_init):
        """Raw interpolation of this made make_url raise, out of init()."""
        url = run_init("postgresql://dbuser@dbhost:5432/anidb_cache", password="p@ss:w/rd#1")

        assert url.password == "p@ss:w/rd#1"
        assert url.host == "dbhost"
        assert url.port == 5432
        assert url.database == "anidb_cache"

    def test_a_username_containing_url_syntax_arrives_intact(self, run_init):
        url = run_init("postgresql://dbhost:5432/anidb_cache", login="user@example.com")

        assert url.username == "user@example.com"
        assert url.password == "s3cret"
        assert url.host == "dbhost"

    def test_an_ordinary_password_is_not_altered(self, run_init):
        """Quoting must be invisible for values that need none."""
        url = run_init("postgresql://dbuser@dbhost:5432/anidb_cache", password="plainpassword")

        assert url.password == "plainpassword"


class TestWhenNothingShouldChange:
    def test_a_url_that_already_has_a_password_is_left_alone(self, run_init):
        url = run_init("postgresql://dbuser:original@dbhost:5432/anidb_cache")

        assert url.password == "original"

    def test_a_url_naming_a_different_user_is_left_alone(self, run_init):
        """netrc holds one credential per host, and pairing it with another user
        would only fail authentication somewhere less obvious."""
        url = run_init("postgresql://someoneelse@dbhost:5432/anidb_cache")

        assert url.password is None
        assert url.username == "someoneelse"

    def test_an_entry_with_no_login_is_left_alone(self, run_init):
        """A password belonging to no user is not a credential for this URL.

        netrc permits `machine X password Y` with no login, and the rule here is
        that the password is only supplied when it belongs to the user the URL
        names. An entry with no login belongs to no user -- it previously matched
        a URL that also named none, and rebuilt it with an empty username.
        """
        url = run_init("postgresql://dbhost:5432/anidb_cache", login=None)

        assert url.password is None
        assert url.username is None

    def test_a_host_with_no_netrc_entry_is_left_alone(self, run_init):
        url = run_init("postgresql://dbuser@otherhost:5432/anidb_cache")

        assert url.password is None

    def test_a_sqlite_url_has_no_host_and_is_left_alone(self, tmp_path, clean_globals, captured_url):
        """The default configuration, and it must not be mangled by a lookup that
        cannot apply to it."""
        original = f"sqlite:///{tmp_path}/cache.db"
        anidb_client.init(original, db_only=True, netrc_file=write_netrc(tmp_path))

        assert captured_url[0] == original


class TestAddressForms:
    def test_an_ipv6_host_keeps_its_brackets(self, run_init):
        """urlparse strips them from `hostname`, so rebuilding has to put them
        back or the result reads as a host called "::1" with a stray port."""
        url = run_init("postgresql://dbuser@[::1]:5432/anidb_cache", machine="::1")

        assert url.host == "::1"
        assert url.port == 5432
        assert url.password == "s3cret"
