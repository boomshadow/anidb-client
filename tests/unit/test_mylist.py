"""Tests for adding files to and removing them from mylist.

Mylist management is the reason this library exists, and it is the one part that
*writes* to AniDB. A wrong command here does not show up as a bad read later --
it changes someone's collection records, and it costs a request against the flood
limit to do so.

The review bot found a bug in this code that had shipped for years: the
multi-episode deletion loop issued the same episode over and over. That is what
these tests are for -- asserting on the commands that actually go out, and on
what the cache holds afterwards.
"""

import datetime

import pytest

from tests import factories
from tests.objectlayer import FakeResponse

UTC = datetime.UTC


@pytest.fixture
def cached_file(anidb, session):
    """A file already in the cache, so nothing has to be fetched to act on it."""
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    # No lid: a file with one takes the "edit an existing entry" path instead,
    # which is covered separately below.
    session.add(factories.make_file(aid=6187, eid=96461, fid=12345, lid=None))
    session.commit()
    return anidb.File(fid=12345)


@pytest.fixture
def generic_file(anidb, session):
    """A generic mylist entry -- no local file, identified by anime and episode.

    This is the "I ripped it myself" case the library was written for, and the one
    whose deletion path was broken.

    The cached row is attached by hand rather than found by the constructor, and
    that is worth recording: `_get_db_data` looks an anime+episode entry up and
    then keeps only rows that already have an lid, so a generic entry without one
    is never loaded. The per-episode deletion branch needs exactly that state --
    a cached row with neither fid nor lid -- which makes it hard to reach through
    ordinary construction, and is a large part of why the bug in it survived.
    """
    from anidb_client.db import FileTable

    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    session.add(factories.make_file(aid=6187, eid=96461, fid=None, lid=None, is_generic=True))
    session.commit()

    f = anidb.File(anime=6187, episode="5")
    f.db_data = session.query(FileTable).one()
    f._is_generic = True
    return f


class TestRemoveFromMylist:
    def test_a_file_with_an_fid_is_removed_by_fid(self, cached_file, link):
        """fid is the most precise identifier, so it wins when present."""
        link.on("MYLISTDEL", FakeResponse("211"))
        cached_file.remove_from_mylist()

        assert link.params_for("MYLISTDEL") == [{"fid": 12345}]

    def test_removal_clears_the_cached_mylist_state(self, cached_file, link, anidb):
        """The local cache has to forget the entry too.

        Otherwise the file still looks like it is in mylist until the cache
        happens to be refreshed, and a later add would be treated as an edit.
        """
        link.on("MYLISTDEL", FakeResponse("211"))
        cached_file.remove_from_mylist()

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            stored = check.query(FileTable).one()
            assert stored.lid is None
            assert stored.mylist_state is None
            assert stored.mylist_viewed is None

    def test_a_file_anidb_never_had_is_reported_but_not_an_error(self, cached_file, link):
        """411 means it was not there. Nothing to do, and nothing to raise about."""
        link.on("MYLISTDEL", FakeResponse("411"))
        cached_file.remove_from_mylist()

        assert link.commands() == ["MYLISTDEL"]

    def test_a_multi_episode_generic_file_deletes_each_episode_once(self, generic_file, link):
        """The bug the review bot found.

        Every iteration built its command from `self.episode.episode_number`
        rather than the loop variable, so a file covering episodes 5 to 7 issued
        three deletions for episode 5 and left 6 and 7 in mylist. The symmetric
        add path does use its loop variable, which is what made it a copy-paste
        slip rather than an intent.
        """
        link.on("MYLISTDEL", FakeResponse("211"))
        generic_file._multiep = ["5", "6", "7"]
        generic_file.remove_from_mylist()

        assert link.params_for("MYLISTDEL") == [
            {"aid": 6187, "epno": "5"},
            {"aid": 6187, "epno": "6"},
            {"aid": 6187, "epno": "7"},
        ]

    def test_a_single_episode_generic_file_deletes_just_that_episode(self, generic_file, link):
        link.on("MYLISTDEL", FakeResponse("211"))
        generic_file.remove_from_mylist()

        assert link.params_for("MYLISTDEL") == [{"aid": 6187, "epno": "5"}]


class TestUpdateMylist:
    def test_an_existing_entry_is_edited_by_lid(self, anidb, session, link):
        """An lid means the entry is already there, so this is an edit, not an add.

        Sending an add for it would be rejected as a duplicate.
        """
        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
        session.add(factories.make_file(aid=6187, eid=96461, fid=12345, lid=555))
        session.commit()

        link.on("MYLISTADD", FakeResponse("311", datalines=[{"entrycnt": "1"}]))
        anidb.File(fid=12345).update_mylist(state="on hdd")

        params = link.params_for("MYLISTADD")[0]
        assert params["lid"] == 555
        assert params["edit"] == 1
        assert "fid" not in params

    def test_a_known_file_is_added_by_fid(self, cached_file, link):
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(state="on hdd")

        params = link.params_for("MYLISTADD")[0]
        assert params["fid"] == 12345
        assert params["state"] == "1", "'on hdd' is state 1 on the wire"

    @pytest.mark.parametrize(
        ("state", "expected"),
        [("unknown", "0"), ("on hdd", "1"), ("on cd", "2"), ("deleted", "3")],
    )
    def test_each_mylist_state_maps_to_its_wire_value(self, cached_file, link, state, expected):
        """These numbers are the protocol. Sending the wrong one silently
        mislabels where someone's file is stored."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(state=state)

        assert link.params_for("MYLISTADD")[0]["state"] == expected

    def test_marking_watched_sends_a_viewed_flag(self, cached_file, link):
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(watched=True)

        assert link.params_for("MYLISTADD")[0]["viewed"] == 1

    def test_a_watched_datetime_is_sent_as_a_timestamp(self, cached_file, link):
        """AniDB takes a unix timestamp, not a formatted date."""
        when = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(watched=when)

        params = link.params_for("MYLISTADD")[0]
        assert params["viewed"] == 1
        assert params["viewdate"] == int(when.timestamp())

    def test_a_multi_episode_generic_file_adds_each_episode(self, generic_file, link):
        """The add path's loop, which is the one that was already correct.

        Pinned so the fix to its sibling cannot be "tidied" into matching the
        broken version.
        """
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        generic_file._multiep = ["5", "6"]
        generic_file.update_mylist(state="on hdd")

        epnos = [p["epno"] for p in link.params_for("MYLISTADD")]
        assert epnos == ["5", "6"]

    def test_a_generic_add_is_marked_generic(self, generic_file, link):
        """Without generic=1 AniDB rejects an add for a file it has no record of."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        generic_file.update_mylist(state="on hdd")

        assert link.params_for("MYLISTADD")[0]["generic"] == 1

    def test_a_rejection_is_logged_rather_than_raised(self, cached_file, link, caplog):
        """AniDB refusing an add is an outcome, not an exception.

        320 is "no such file"; the call completes and the caller carries on.
        """
        link.on("MYLISTADD", FakeResponse("320"))
        with caplog.at_level("WARNING", logger="anidb_client.test"):
            cached_file.update_mylist(state="on hdd")

        assert "Could not add file" in caplog.text

    def test_a_multi_entry_reply_stores_the_returned_lid(self, cached_file, link, anidb):
        """When AniDB reports more than one entry the number is actually the lid.

        Inherited behaviour, and surprising enough to be worth a test: the same
        field means two different things depending on its value.
        """
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "7788"}]))
        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.query(FileTable).one().lid == 7788

    def test_the_entries_field_is_preferred_over_entrycnt(self, cached_file, link, anidb):
        """Two spellings of the same field; the first one named wins, as before."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entries": "4321", "entrycnt": "9999"}]))
        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.query(FileTable).one().lid == 4321

    def test_a_reply_naming_neither_count_field_completes(self, cached_file, link):
        """A hang, before. The count was assigned back over `res` itself, so when
        the reply carried neither field the comparison that followed ran against
        the Response object and raised TypeError -- on the response thread, which
        skipped wait.set() and left update_mylist() blocked for good.
        """
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"something_else": "1"}]))

        # Returning at all is the assertion: under the old code this call did not.
        cached_file.update_mylist(state="on hdd")

        assert len(link.requests_for("MYLISTADD")) == 1

    def test_a_reply_with_no_data_lines_at_all_completes(self, cached_file, link, anidb):
        """Same hang by a different route: datalines[0] on an empty list."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[]))

        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.query(FileTable).one().lid is None, "nothing to read means nothing to store"

    def test_a_single_entry_reply_does_not_overwrite_the_lid(self, cached_file, link, anidb):
        """One entry means the number is a count, not an lid. Storing it would
        put a 1 in the lid column and make the next add look like an edit."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.query(FileTable).one().lid is None
