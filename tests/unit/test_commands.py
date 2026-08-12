"""Tests for command construction and serialisation.

Two distinct concerns live here. Serialisation is protocol-critical: the wire
format is `COMMAND key=value&key=value`, so any literal `&` inside a value must be
escaped or it splits the command into fields the server reads as something else.
Parameter validation is quota-critical: AniDB counts malformed requests against
you, so combinations the API rejects are caught locally instead of being sent.
"""

import pytest

from anidb_client.commands import (
    AnimeCommand,
    AuthCommand,
    BuddyAddCommand,
    Command,
    EncodingCommand,
    EpisodeCommand,
    FileCommand,
    GroupCommand,
    LogoutCommand,
    MyListAddCommand,
    MyListCommand,
    MyListDelCommand,
    PingCommand,
    SendMsgCommand,
    VoteCommand,
)
from anidb_client.errors import AniDBCommandTimeoutError, AniDBIncorrectParameterError


class TestSerialisation:
    def test_parameters_are_joined_with_ampersands(self):
        cmd = Command("ANIME", aid=1, aname="x")
        assert cmd.raw_data() == "ANIME aid=1&aname=x"

    def test_command_with_no_parameters_still_has_a_trailing_space(self):
        """Inherited quirk: flatten() always joins the verb and the field string.

        LOGOUT therefore goes out as "LOGOUT " rather than "LOGOUT". The server
        accepts it, and it is pinned here so a future tidy-up is a deliberate
        protocol change rather than an accident.
        """
        assert LogoutCommand().raw_data() == "LOGOUT "

    def test_none_valued_parameters_are_omitted(self):
        """Optional fields must vanish rather than be sent as the string 'None'."""
        cmd = Command("ANIME", aid=1, aname=None, amask=None)
        assert cmd.raw_data() == "ANIME aid=1"

    def test_ampersands_in_values_are_escaped(self):
        """An unescaped & would terminate the value and start a bogus field."""
        cmd = Command("GROUP", gname="Rock & Roll")
        assert cmd.raw_data() == "GROUP gname=Rock &amp; Roll"

    def test_ampersands_in_keys_are_escaped(self):
        assert Command("X", **{"a&b": "c"}).raw_data() == "X a&amp;b=c"

    def test_escaping_is_applied_to_every_occurrence(self):
        assert Command("X", v="a&b&c").raw_data() == "X v=a&amp;b&amp;c"

    def test_non_string_values_are_stringified(self):
        assert Command("X", n=42, f=1.5, b=True).raw_data() == "X n=42&f=1.5&b=True"

    def test_authorize_adds_the_tag_and_session(self):
        cmd = Command("ANIME", aid=1)
        cmd.tag = "T001"
        cmd.authorize("sesskey")
        assert cmd.parameters["tag"] == "T001"
        assert cmd.parameters["s"] == "sesskey"
        assert "tag=T001" in cmd.raw_data()
        assert "s=sesskey" in cmd.raw_data()

    def test_raw_data_reflects_parameters_changed_after_construction(self):
        """raw_data() re-flattens, so authorize() after construction is visible."""
        cmd = Command("ANIME", aid=1)
        cmd.parameters["aid"] = 2
        assert cmd.raw_data() == "ANIME aid=2"

    def test_encoding_command_sends_the_encoding_it_was_given(self):
        """Regression: the parameter dict was built as {"name": type}.

        That is the builtin `type`, not the argument, so the command serialised
        as "ENCODING name=<class 'type'>" and the caller's encoding was silently
        discarded.
        """
        assert EncodingCommand("utf8").raw_data() == "ENCODING name=utf8"

    def test_auth_command_carries_the_client_identity(self):
        """The name/version pair AniDB matches against its client registry."""
        raw = AuthCommand("user", "pw", 3, "anidbclientpy", 1).raw_data()
        assert raw.startswith("AUTH ")
        assert "client=anidbclientpy" in raw
        assert "clientver=1" in raw
        assert "protover=3" in raw
        # comp/enc default on, so replies may arrive deflated and as utf-8.
        assert "comp=1" in raw
        assert "enc=utf8" in raw


class TestParameterValidation:
    """Each case is a combination the UDP API documents as invalid.

    Sending one costs a request against the flood limit and returns an error, so
    every one of these is rejected before a packet is built.
    """

    def test_anime_requires_an_identifier(self):
        with pytest.raises(AniDBIncorrectParameterError, match="a\\(id\\|name\\)"):
            AnimeCommand()

    @pytest.mark.parametrize("kwargs", [{"aid": 1}, {"aname": "x"}])
    def test_anime_accepts_either_identifier(self, kwargs):
        assert AnimeCommand(**kwargs).command == "ANIME"

    def test_episode_requires_eid_or_anime_plus_epno(self):
        with pytest.raises(AniDBIncorrectParameterError):
            EpisodeCommand()

    def test_episode_rejects_both_aid_and_aname(self):
        with pytest.raises(AniDBIncorrectParameterError):
            EpisodeCommand(aid=1, aname="x", epno=1)

    def test_episode_rejects_eid_combined_with_anything_else(self):
        with pytest.raises(AniDBIncorrectParameterError):
            EpisodeCommand(eid=1, aid=2)

    def test_episode_accepts_eid_alone(self):
        assert EpisodeCommand(eid=96461).command == "EPISODE"

    def test_episode_accepts_anime_plus_epno(self):
        assert EpisodeCommand(aid=6187, epno=5).command == "EPISODE"

    def test_file_requires_one_of_three_identification_routes(self):
        with pytest.raises(AniDBIncorrectParameterError):
            FileCommand()

    def test_file_rejects_size_without_ed2k(self):
        """Size alone cannot identify a file; the pair is what AniDB looks up."""
        with pytest.raises(AniDBIncorrectParameterError):
            FileCommand(size=123)

    def test_file_rejects_mixing_fid_with_a_hash_lookup(self):
        with pytest.raises(AniDBIncorrectParameterError):
            FileCommand(fid=1, size=123, ed2k="abc")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"fid": 1},
            {"size": 123, "ed2k": "d41d8cd98f00b204e9800998ecf8427e"},
            {"aid": 1, "gid": 2, "epno": 3},
        ],
        ids=["by-fid", "by-size-and-hash", "by-anime-group-episode"],
    )
    def test_file_accepts_each_documented_route(self, kwargs):
        assert FileCommand(**kwargs).command == "FILE"

    def test_group_requires_exactly_one_identifier(self):
        with pytest.raises(AniDBIncorrectParameterError):
            GroupCommand()
        with pytest.raises(AniDBIncorrectParameterError):
            GroupCommand(gid=1, gname="x")

    def test_mylist_rejects_lid_combined_with_other_selectors(self):
        with pytest.raises(AniDBIncorrectParameterError):
            MyListCommand(lid=1, fid=2)

    def test_mylistadd_requires_edit_when_given_lid(self):
        """A bare lid means 'add', but an lid only exists for an entry already added."""
        with pytest.raises(AniDBIncorrectParameterError):
            MyListAddCommand(lid=1)
        assert MyListAddCommand(lid=1, edit=1).command == "MYLISTADD"

    def test_mylistdel_requires_a_selector(self):
        with pytest.raises(AniDBIncorrectParameterError):
            MyListDelCommand()

    def test_vote_requires_exactly_one_of_id_or_name(self):
        with pytest.raises(AniDBIncorrectParameterError):
            VoteCommand(type=1)
        with pytest.raises(AniDBIncorrectParameterError):
            VoteCommand(type=1, id=1, name="x")

    def test_buddyadd_requires_exactly_one_identifier(self):
        with pytest.raises(AniDBIncorrectParameterError):
            BuddyAddCommand()

    def test_buddyadd_lowercases_the_username(self):
        """AniDB usernames are case-insensitive; normalising avoids a cache miss."""
        assert BuddyAddCommand(uname="MixedCase").parameters["uname"] == "mixedcase"

    def test_buddyadd_accepts_a_uid_with_no_username(self):
        """The other half of the XOR the guard above accepts.

        This lowercased `uname` unconditionally, so identifying a buddy by id --
        which the guard explicitly permits -- raised AttributeError on None
        instead of building the command.
        """
        cmd = BuddyAddCommand(uid=42)

        assert cmd.parameters["uid"] == 42
        assert cmd.raw_data() == "BUDDYADD uid=42"

    @pytest.mark.parametrize(
        ("title", "body"),
        [("x" * 51, "ok"), ("ok", "x" * 901)],
        ids=["title-too-long", "body-too-long"],
    )
    def test_sendmsg_enforces_the_documented_length_limits(self, title, body):
        with pytest.raises(AniDBIncorrectParameterError, match="50 chars"):
            SendMsgCommand("someone", title, body)

    def test_sendmsg_accepts_the_maximum_permitted_lengths(self):
        """Boundary check: 50 and 900 are allowed, 51 and 901 are not."""
        assert SendMsgCommand("someone", "x" * 50, "y" * 900).command == "SENDMSG"


class FakeLink:
    """Records what a command asks the transport to do on a timeout."""

    def __init__(self):
        self.events = []

    def request(self, command, callback, prio=False):
        self.events.append(("request", prio))
        # The real transport counts an attempt when the command reaches the
        # socket, not when it is queued. Standing in for that here is what makes
        # the budget in these tests the same budget the transport enforces.
        command.attempts += 1
        return command.future

    def set_banned(self, code, reason=None):
        self.events.append(("banned", code))


class TestRetryPolicy:
    """The budget is spent, not renewed.

    The previous policy decremented a counter, and on reaching zero backed off,
    **restored the counter** and re-sent. So a command AniDB would never answer
    was re-sent for the life of the process -- slower each round, because the
    back-off grew, but with no branch anywhere that stopped. A caller waiting on
    it waited forever, which is the reported hang.
    """

    def test_a_command_starts_with_no_attempts_spent(self):
        assert PingCommand().attempts == 0

    def test_a_timeout_inside_the_budget_is_retried(self):
        cmd = PingCommand()
        cmd.callback = None
        cmd.attempts = 1
        link = FakeLink()

        cmd.handle_timeout(link)

        assert link.events == [("request", True)]
        assert not cmd.future.done(), "a command still being retried has no outcome yet"

    def test_the_budget_runs_out(self):
        """The branch that did not exist: after the last attempt, stop.

        Both halves matter. The command fails, so its caller is told rather than
        left waiting; and the transport backs off, so the next command is not
        sent into an API that has just failed to answer three of them.
        """
        cmd = PingCommand()
        cmd.callback = None
        cmd.attempts = Command.MAX_ATTEMPTS
        link = FakeLink()

        cmd.handle_timeout(link)

        assert ("request", True) not in link.events, "a spent command was sent again"
        assert link.events == [("banned", 604)]
        with pytest.raises(AniDBCommandTimeoutError):
            cmd.future.result(timeout=0)

    def test_the_caller_is_told_before_the_back_off_begins(self):
        """Order matters, because backing off means sleeping.

        The decision to give up has already been made by the time the back-off
        starts. Telling the caller afterwards would hold it for the length of a
        back-off -- half an hour and up -- to deliver an answer that was ready
        immediately.
        """
        cmd = PingCommand()
        cmd.callback = None
        cmd.attempts = Command.MAX_ATTEMPTS
        settled = []

        class SlowBanLink(FakeLink):
            def set_banned(self, code, reason=None):
                settled.append(cmd.future.done())
                super().set_banned(code, reason)

        cmd.handle_timeout(SlowBanLink())

        assert settled == [True], "the caller was still waiting when the back-off started"

    def test_a_command_retried_to_exhaustion_reaches_the_wire_a_bounded_number_of_times(self):
        """End to end over the whole budget: it terminates, and it terminates soon.

        Driven through the same counting the transport does, so this is the real
        number of times AniDB would be asked -- the number that matters to a
        service that bans clients for asking too often.
        """
        cmd = PingCommand()
        cmd.callback = None
        link = FakeLink()

        link.request(cmd, None)  # the first send
        rounds = 0
        while not cmd.future.done():
            rounds += 1
            assert rounds <= 10, "handle_timeout never reached a terminal branch"
            cmd.handle_timeout(link)

        sends = [event for event in link.events if event[0] == "request"]
        assert len(sends) == Command.MAX_ATTEMPTS
        assert cmd.attempts == Command.MAX_ATTEMPTS
