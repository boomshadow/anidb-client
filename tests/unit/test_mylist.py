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
from sqlalchemy import select

from anidb_client.errors import AniDBBannedError, AniDBCommandTimeoutError, AniDBIncorrectParameterError
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
    f.db_data = session.scalars(select(FileTable)).one()
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

            stored = check.scalars(select(FileTable)).one()
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

    def test_a_ranged_generic_file_deletes_each_episode(self, anidb, session, link):
        """The mirror of `test_a_ranged_generic_file_sends_string_episode_numbers`.

        The delete loop read `self._multiep` -- the *cached* result of the property,
        which is None until something reads it -- with its own fallback to the raw
        episode number. So a ranged generic file nobody had asked `multiep` about
        sent one MYLISTDEL for epno "5-7", which matches no entry AniDB holds per
        episode: the removal reported success and removed nothing, while the add
        path on the same object had created three entries.

        Built from an `Episode` object rather than an epno string for the same
        reason the add test is: the string form seeds `_multiep` from the argument,
        which is exactly the state this bug hides behind.
        """
        from anidb_client.db import FileTable

        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96480, epno="5-7"))
        session.add(factories.make_file(aid=6187, eid=96480, fid=None, lid=None, is_generic=True))
        session.commit()
        ranged = anidb.File(anime=6187, episode=anidb.Episode(eid=96480))
        ranged.db_data = session.scalars(select(FileTable)).one()
        ranged._is_generic = True
        assert ranged._multiep is None, "the fallback under test is only reached while this is unset"

        link.on("MYLISTDEL", FakeResponse("211"))
        ranged.remove_from_mylist()

        assert link.params_for("MYLISTDEL") == [
            {"aid": 6187, "epno": "5"},
            {"aid": 6187, "epno": "6"},
            {"aid": 6187, "epno": "7"},
        ]

    def test_a_path_backed_files_removal_does_not_consult_its_filename(self, anidb, session, link, tmp_path):
        """The half of the asymmetry that is deliberately kept.

        `multiep` has a third branch: given a local path it may adopt the episode
        set guessed from the filename. Expanding the range fixes the reported
        defect without that, and a filename must not get to decide what is deleted
        from someone's mylist. This pins that the narrow fix stayed narrow -- the
        filename names episodes 5 to 7, and the removal still names only the
        episode the cache believes the file to be.
        """
        from anidb_client.db import FileTable

        path = tmp_path / "Kemono no Souja Erin - 05-07.mkv"
        path.write_bytes(b"")
        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
        session.add(factories.make_file(aid=6187, eid=96461, fid=None, lid=None, is_generic=True))
        session.commit()
        f = anidb.File(path=str(path))
        f.db_data = session.scalars(select(FileTable)).one()
        f._anime = anidb.Anime(6187)
        f._episode = anidb.Episode(eid=96461)
        f._is_generic = True

        link.on("MYLISTDEL", FakeResponse("211"))
        f.remove_from_mylist()

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

    def test_a_ranged_generic_file_sends_string_episode_numbers(self, anidb, session, link):
        """The add loop reads the `multiep` property, not the cached attribute.

        Its multi-episode branch used to answer with a `range`, so this one path
        put ints on the wire where every other path -- including the sibling test
        above, which sets the attribute directly and so never reaches the property
        -- puts strings. AniDB records a file spanning several episodes as a single
        episode row with a ranged epno, so this is the ordinary shape for the case,
        not an edge.
        """
        from anidb_client.db import FileTable

        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96480, epno="5-7"))
        session.add(factories.make_file(aid=6187, eid=96480, fid=None, lid=None, is_generic=True))
        session.commit()
        # Built from an Episode object rather than an epno string: the string form
        # seeds `_multiep` from the argument, which short-circuits the property
        # this test is about.
        ranged = anidb.File(anime=6187, episode=anidb.Episode(eid=96480))
        ranged.db_data = session.scalars(select(FileTable)).one()
        ranged._is_generic = True

        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        ranged.update_mylist(state="on hdd")

        assert [p["epno"] for p in link.params_for("MYLISTADD")] == ["5", "6", "7"]

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

            assert check.scalars(select(FileTable)).one().lid == 7788

    def test_the_entries_field_is_preferred_over_entrycnt(self, cached_file, link, anidb):
        """Two spellings of the same field; the first one named wins, as before."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entries": "4321", "entrycnt": "9999"}]))
        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.scalars(select(FileTable)).one().lid == 4321

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

            assert check.scalars(select(FileTable)).one().lid is None, "nothing to read means nothing to store"

    def test_a_single_entry_reply_does_not_overwrite_the_lid(self, cached_file, link, anidb):
        """One entry means the number is a count, not an lid. Storing it would
        put a 1 in the lid column and make the next add look like an edit."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))
        cached_file.update_mylist(state="on hdd")

        with anidb.get_session() as check:
            from anidb_client.db import FileTable

            assert check.scalars(select(FileTable)).one().lid is None


class TestMylistWritesThatCannotReachAniDB:
    """A write that quietly fails is worse than a read that quietly fails.

    A read that gives up costs a stale value. A mylist write that gives up leaves
    the caller believing it changed someone's collection records when it did not
    -- and, before this, leaves it believing that forever, because the call never
    returned at all.

    Distinct from a rejection *by* AniDB, which is still logged rather than
    raised: that request arrived and was answered, and the answer was no.
    """

    def test_a_banned_add_raises(self, cached_file, link):
        link.fails("MYLISTADD", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError):
            cached_file.update_mylist(state="on hdd")

    def test_an_unanswered_add_raises(self, cached_file, link):
        link.fails("MYLISTADD", AniDBCommandTimeoutError("MYLISTADD went unanswered"))

        with pytest.raises(AniDBCommandTimeoutError):
            cached_file.update_mylist(state="on hdd")

    def test_a_banned_removal_raises(self, cached_file, link):
        link.fails("MYLISTDEL", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError):
            cached_file.remove_from_mylist()

    def test_a_banned_removal_of_a_generic_file_raises_on_the_first_episode(self, generic_file, link):
        """The per-episode loop must stop, not carry on into a banned API.

        Each iteration is another request, and continuing to send them after
        being told to stop is the behaviour that lengthens a ban.
        """
        link.fails("MYLISTDEL", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError):
            generic_file.remove_from_mylist()

        assert len(link.requests_for("MYLISTDEL")) == 1

    def test_a_rejection_by_anidb_is_still_not_an_error(self, cached_file, link, caplog):
        """The line between "AniDB said no" and "we never asked"."""
        link.on("MYLISTADD", FakeResponse("320"))

        cached_file.update_mylist(state="on hdd")


class TestAddingAGenericEntryWithoutAFile:
    """`Episode.add_to_mylist()`: the file-less add, for a collection AniDB cannot see.

    The reason this exists beside `update_mylist()` rather than inside it is the
    one-entry-per-episode rule: that rule is enforced by *removing* whatever
    already covers the episode, which is the correct thing for a file that is
    replacing another and the wrong thing entirely for "record that I have this".
    Most of what follows is about the difference -- that this path adds, only
    adds, and can be run twice.
    """

    def test_one_episode_is_one_request_naming_the_anime_and_the_episode(self, anidb, link):
        """The whole command, asserted whole.

        Equality rather than a field at a time, because what is *absent* is the
        point: no lid, no fid, and above all no `edit`, which is what makes this
        unable to overwrite an entry even in principle.
        """
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert link.params_for("MYLISTADD") == [{"aid": 6187, "generic": 1, "epno": "5", "state": "1"}]

    def test_nothing_is_sent_but_the_add(self, anidb, link):
        """No probe before it and no read after it.

        `update_mylist()` spends a lookup deciding whether an entry exists and
        another afterwards recovering the identifier of the one it made. Neither
        is needed to add a generic entry, and a season's worth of them is the
        difference between two dozen requests and eighty against an API that bans
        clients for asking too often.
        """
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert link.commands() == ["MYLISTADD"]

    def test_a_special_is_sent_in_anidbs_own_episode_vocabulary(self, anidb, link):
        """S1 is episode 1 of the specials, not episode 1. The prefix is the whole
        distinction, and dropping it would file a special as a regular episode."""
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        result = anidb.Episode(anime=6187, epno="S1").add_to_mylist(state="on hdd")

        assert link.params_for("MYLISTADD")[0]["epno"] == "S1"
        assert result.episode_number == "S1"

    @pytest.mark.parametrize(
        ("state", "expected"),
        [("unknown", "0"), ("on hdd", "1"), ("on cd", "2"), ("deleted", "3")],
    )
    def test_each_mylist_state_maps_to_its_wire_value(self, anidb, link, state, expected):
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        anidb.Episode(anime=6187, epno="5").add_to_mylist(state=state)

        assert link.params_for("MYLISTADD")[0]["state"] == expected

    def test_a_watched_datetime_is_sent_as_a_timestamp(self, anidb, link):
        when = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd", watched=when)

        params = link.params_for("MYLISTADD")[0]
        assert params["viewed"] == 1
        assert params["viewdate"] == int(when.timestamp())

    def test_an_entry_that_did_not_exist_reports_added(self, anidb, link):
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}], resstr="MYLIST ENTRY ADDED"))

        result = anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert result.outcome is anidb.MylistAddOutcome.ADDED
        assert (result.aid, result.episode_number) == (6187, "5")
        assert result.rescode == "210"
        assert result.lid is None, "a generic add is answered with a count of entries, never an identifier"

    def test_an_entry_that_already_existed_reports_it_and_changes_nothing(self, anidb, link):
        """The repeat case, and the one that has to be a result rather than an error.

        A caller retried after a crash will meet this for everything it already
        did. AniDB refuses the duplicate and returns the entry it already holds --
        which is also the guarantee that a real, file-backed entry someone added
        from another client is still there afterwards.
        """
        link.on(
            "MYLISTADD",
            FakeResponse("310", datalines=[{"lid": "9876", "eid": "96461"}], resstr="FILE ALREADY IN MYLIST"),
        )

        result = anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert result.outcome is anidb.MylistAddOutcome.ALREADY_PRESENT
        assert result.lid == 9876
        assert link.commands() == ["MYLISTADD"], "nothing is sent to make room for an entry we did not add"

    def test_an_existing_entry_with_no_usable_identifier_reports_none(self, anidb, link):
        """AniDB can answer a generic entry's identifier as 0.

        A 0 passed through would read like an id to a caller storing it, and
        would be one AniDB does not recognise. Absent is the honest answer.
        """
        link.on("MYLISTADD", FakeResponse("310", datalines=[{"lid": "0"}]))

        result = anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert result.outcome is anidb.MylistAddOutcome.ALREADY_PRESENT
        assert result.lid is None

    def test_an_existing_entry_reported_without_any_fields_still_completes(self, anidb, link):
        link.on("MYLISTADD", FakeResponse("310", datalines=[]))

        result = anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert result.outcome is anidb.MylistAddOutcome.ALREADY_PRESENT
        assert result.lid is None

    @pytest.mark.parametrize(
        ("rescode", "resstr"),
        [("330", "NO SUCH ANIME"), ("340", "NO SUCH EPISODE"), ("320", "NO SUCH FILE")],
    )
    def test_a_refusal_by_anidb_is_a_result_not_an_exception(self, anidb, link, rescode, resstr):
        """The request arrived and was answered; the answer was no.

        Which "no" it was is the caller's diagnostic -- an anime AniDB does not
        have is a different problem from an episode number that anime does not
        have -- so the code and its text are carried through rather than
        flattened into a single failure.
        """
        link.on("MYLISTADD", FakeResponse(rescode, resstr=resstr))

        result = anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

        assert result.outcome is anidb.MylistAddOutcome.REJECTED
        assert (result.rescode, result.reason) == (rescode, resstr)

    def test_a_banned_add_raises(self, anidb, link):
        """A write the transport could not deliver is not a rejection.

        The distinction the whole result type rests on: AniDB said no is data,
        we never got to ask is an exception.
        """
        link.fails("MYLISTADD", AniDBBannedError("555 BANNED"))

        with pytest.raises(AniDBBannedError):
            anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

    def test_an_unanswered_add_raises(self, anidb, link):
        link.fails("MYLISTADD", AniDBCommandTimeoutError("MYLISTADD went unanswered"))

        with pytest.raises(AniDBCommandTimeoutError):
            anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd")

    @pytest.mark.parametrize("epno", ["0", "-12", "5-7", "   ", "S", "1.5"])
    def test_an_episode_number_that_is_not_one_episode_is_refused_before_anything_is_sent(self, anidb, link, epno):
        """The guard that stops a one-episode call becoming a whole-series one.

        MYLISTADD overloads this field: absent or zero means *every episode of
        the anime*, and a negative number means every episode up to it. So an
        unset variable upstream does not fail quietly here -- it writes several
        hundred entries into someone's list, against an API with no bulk undo. A
        range is refused separately because AniDB does not define what this
        command does with one.

        Refused locally, so it costs no request either.
        """
        with pytest.raises(AniDBIncorrectParameterError):
            anidb.Episode(anime=6187, epno=epno).add_to_mylist(state="on hdd")

        assert link.commands() == []

    def test_a_state_that_is_not_a_state_is_refused_before_anything_is_sent(self, anidb, link):
        """A typo used to select nothing and be dropped from the command.

        The add then succeeded and AniDB filed the entry under its own default,
        so the caller was told the state it asked for had been recorded when a
        different one had. Carrying the state is the entire point of the call.
        """
        with pytest.raises(AniDBIncorrectParameterError) as raised:
            anidb.Episode(anime=6187, epno="5").add_to_mylist(state="on hdd ")

        assert "'on hdd'" in str(raised.value), "the message names the vocabulary that would have worked"
        assert link.commands() == []

    def test_no_state_at_all_sends_none_and_lets_anidb_default(self, anidb, link):
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        anidb.Episode(anime=6187, epno="5").add_to_mylist()

        assert "state" not in link.params_for("MYLISTADD")[0]

    def test_the_local_cache_is_not_written(self, anidb, session, link):
        """The documented limitation, pinned so it stays a decision.

        AniDB returns no identifier for a file-less entry, and this library's
        cached mylist rows are built around one -- so there is nothing to write a
        faithful row from, and a row written without one is invisible to every
        reader anyway. The consequence a caller feels is here in full:
        `in_mylist` does not know about the entry until something refreshes it
        from AniDB. Closing that costs a second request per episode and is the
        obvious next iteration; it is not this one.
        """
        from anidb_client.db import FileTable

        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
        session.commit()
        link.on("MYLISTADD", FakeResponse("210", datalines=[{"entrycnt": "1"}]))

        episode = anidb.Episode(anime=6187, epno="5")
        assert episode.add_to_mylist(state="on hdd").outcome is anidb.MylistAddOutcome.ADDED

        with anidb.get_session() as check:
            assert check.scalars(select(FileTable)).all() == []
        assert anidb.Episode(anime=6187, epno="5").in_mylist is False
