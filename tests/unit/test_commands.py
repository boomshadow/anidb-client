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
from anidb_client.errors import AniDBIncorrectParameterError


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


class TestRetryPolicy:
    def test_a_command_starts_with_two_retries(self):
        assert PingCommand().retries == 2

    def test_handle_timeout_requeues_and_decrements(self):
        calls = []

        class FakeLink:
            def request(self, command, callback, prio=False):
                calls.append((command, prio))

            def set_banned(self, code, reason=None):
                calls.append(("banned", code))

        cmd = PingCommand()
        cmd.callback = None
        link = FakeLink()

        cmd.handle_timeout(link)
        assert cmd.retries == 1
        assert calls[-1] == (cmd, True)

    def test_exhausted_retries_back_off_before_requeueing(self):
        """After the last retry the link is told to back off, not just retried again.

        This is what stops a dead API turning into an unbounded retry loop that
        would itself look like abuse.
        """
        events = []

        class FakeLink:
            def request(self, command, callback, prio=False):
                events.append("request")

            def set_banned(self, code, reason=None):
                events.append(f"banned:{code}")

        cmd = PingCommand()
        cmd.callback = None
        cmd.retries = 0
        cmd.handle_timeout(FakeLink())

        assert events == ["banned:604", "request"]
        assert cmd.retries == 2, "retry budget is restored for the next attempt"
