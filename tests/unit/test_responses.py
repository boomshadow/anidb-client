"""Tests for parsing AniDB UDP replies.

A reply is a text packet: a status line, then zero or more pipe-delimited data
lines. The status line optionally begins with the tag the client sent, which is
how a reply is matched to the request that asked for it -- UDP gives no ordering
guarantee, so the tag is the only correlation there is.

These are the largest untested surface in the library, and the failure mode is
quiet: a mis-split line does not raise, it writes shifted values into the cache.
"""

import ast
import pathlib

import pytest

import anidb_client.responses
from anidb_client.commands import AnimeCommand, AuthCommand, Command
from anidb_client.mapper import getAnimeBitsA, getAnimeCodesA
from anidb_client.responses import (
    AnimeResponse,
    BannedResponse,
    LoginAcceptedResponse,
    NoSuchAnimeResponse,
    ResponseResolver,
    responses,
)


class TestResponseResolver:
    def test_tagged_status_line_is_split_into_tag_code_and_text(self):
        resolver = ResponseResolver(b"T001 200 LOGIN ACCEPTED\n")
        assert resolver.restag == "T001"
        assert resolver.rescode == "200"
        assert resolver.resstr == "LOGIN ACCEPTED"

    def test_untagged_status_line_has_no_tag(self):
        """Untagged replies are unsolicited -- typically a ban or server notice.

        The transport relies on restag being None to recognise that there is no
        pending command to hand the reply to.
        """
        resolver = ResponseResolver(b"555 BANNED\n")
        assert resolver.restag is None
        assert resolver.rescode == "555"
        assert resolver.resstr == "BANNED"

    def test_data_lines_are_split_on_pipes(self):
        resolver = ResponseResolver(b"T002 230 ANIME\n6187|50|2009|TV Series\n")
        assert resolver.datalines == [["6187", "50", "2009", "TV Series"]]

    def test_multiple_data_lines_are_kept_separate(self):
        resolver = ResponseResolver(b"T003 233 ANIME DESC\nline|one\nline|two\n")
        assert resolver.datalines == [["line", "one"], ["line", "two"]]

    def test_a_reply_with_no_data_lines_has_none(self):
        assert ResponseResolver(b"T004 330 NO SUCH ANIME\n").datalines == []

    def test_empty_fields_are_preserved_as_empty_strings(self):
        """Optional AniDB fields arrive as empty columns, not missing ones.

        Collapsing them would shift every later column by one.
        """
        resolver = ResponseResolver(b"T005 230 ANIME\n6187||2009||TV\n")
        assert resolver.datalines == [["6187", "", "2009", "", "TV"]]

    def test_utf8_payloads_decode(self):
        resolver = ResponseResolver("T006 230 ANIME\n獣の奏者 エリン|Erin\n".encode())
        assert resolver.datalines == [["獣の奏者 エリン", "Erin"]]

    def test_status_text_may_itself_contain_spaces(self):
        resolver = ResponseResolver(b"T007 505 ILLEGAL INPUT OR ACCESS DENIED\n")
        assert resolver.rescode == "505"
        assert resolver.resstr == "ILLEGAL INPUT OR ACCESS DENIED"

    def test_resolve_maps_the_code_to_its_response_class(self):
        resolver = ResponseResolver(b"T008 330 NO SUCH ANIME\n")
        assert isinstance(resolver.resolve(AnimeCommand(aid=1)), NoSuchAnimeResponse)

    def test_resolve_of_an_unknown_code_raises(self):
        """An unmapped code must not be silently treated as success.

        The transport logs and stops on this rather than acting on a reply it
        cannot interpret.
        """
        resolver = ResponseResolver(b"T009 999 SOMETHING NEW\n")
        with pytest.raises(KeyError):
            resolver.resolve(AnimeCommand(aid=1))


class TestResponseCodeTable:
    def test_every_mapped_code_is_a_three_digit_string(self):
        for code in responses:
            assert code.isdigit() and len(code) == 3, code

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("200", "LoginAcceptedResponse"),
            ("330", "NoSuchAnimeResponse"),
            ("403", "NotLoggedInResponse"),
            ("500", "LoginFailedResponse"),
            ("501", "LoginFirstResponse"),
            ("503", "ClientVersionOutdatedResponse"),
            ("504", "ClientBannedResponse"),
            ("506", "InvalidSessionResponse"),
            ("555", "BannedResponse"),
            ("601", "AnidbOutOfServiceResponse"),
        ],
    )
    def test_the_codes_the_transport_branches_on_are_mapped(self, code, expected):
        """link.py switches on these specific codes to decide reauth and backoff.

        If one were dropped from the table, resolve() would raise KeyError on a
        real reply and the client would stop rather than back off.
        """
        assert responses[code].__name__ == expected


class TestResponseParsing:
    def test_login_accepted_extracts_session_key_and_nat_address(self):
        """With nat=1 the reply carries the address the server saw.

        The transport compares that port against the local one to detect NAT and
        start sending keepalives, so both fields have to land in attrs.
        """
        cmd = AuthCommand("user", "pw", 3, "anidbclientpy", 1, nat=1)
        resolver = ResponseResolver(b"T001 200 sess1234 203.0.113.7:41234 LOGIN ACCEPTED\n")
        resp = resolver.resolve(cmd)
        resp.parse()

        assert isinstance(resp, LoginAcceptedResponse)
        assert resp.attrs["sesskey"] == "sess1234"
        assert resp.attrs["address"] == "203.0.113.7:41234"
        assert resp.resstr == "LOGIN ACCEPTED"

    def test_login_accepted_without_nat_extracts_only_the_session_key(self):
        cmd = AuthCommand("user", "pw", 3, "anidbclientpy", 1)
        resolver = ResponseResolver(b"T001 200 sess1234 LOGIN ACCEPTED\n")
        resp = resolver.resolve(cmd)
        resp.parse()

        assert resp.attrs == {"sesskey": "sess1234"}
        assert "address" not in resp.attrs

    def test_anime_data_columns_are_named_by_the_requested_mask(self):
        """The reply is positional; only the mask the client sent names the columns.

        This is the seam between mapper and responses: the same mask that
        selected the fields must decode them in the same order, or every value
        is attributed to the wrong field.
        """
        wanted = ["aid", "year", "type", "nr_of_episodes"]
        amask = getAnimeBitsA(wanted)
        cmd = AnimeCommand(aid=6187, amask=amask)

        # Columns arrive in bitmask order -- the order of the field map -- not in
        # the order the caller happened to list them. Pinned explicitly, because
        # this ordering is the whole contract between the mask and the decoder.
        assert getAnimeCodesA(amask) == ["aid", "year", "type", "nr_of_episodes"]

        resolver = ResponseResolver(b"T001 230 ANIME\n6187|2009|TV Series|50\n")
        resp = resolver.resolve(cmd)
        resp.parse()

        assert isinstance(resp, AnimeResponse)
        assert set(resp.datalines[0]) == set(wanted)
        assert resp.datalines[0]["aid"] == "6187"
        assert resp.datalines[0]["year"] == "2009"
        assert resp.datalines[0]["type"] == "TV Series"
        assert resp.datalines[0]["nr_of_episodes"] == "50"

    @pytest.mark.parametrize(
        ("code", "text"),
        [("246", "NOTIFICATION ITEM ADDED"), ("248", "NOTIFICATION ITEM UPDATED")],
    )
    def test_single_field_payloads_are_named_not_split_into_characters(self, code, text):
        """Regression: these two declared `codetail = "nid"` -- a bare string.

        A string is iterable, so dict(zip(codetail, rawline)) paired it character
        by character and produced {"n": ..., "i": ..., "d": ...} rather than a
        single "nid" field. Every other response class uses a tuple; these two
        were missing the trailing comma.
        """
        resolver = ResponseResolver(f"T001 {code} {text}\n4242\n".encode())
        resp = resolver.resolve(Command("NOTIFICATIONADD", aid=1))
        resp.parse()

        assert resp.datalines == [{"nid": "4242"}]

    def test_no_response_class_assigns_a_bare_string_to_a_field_name_tuple(self):
        """A bare string in any code* attribute splits into characters.

        Swept across the whole module rather than only the two that were wrong,
        because the failure is silent: it yields plausible-looking single-letter
        keys instead of raising.

        Read from the source with ast rather than from the classes, because these
        are instance attributes assigned in __init__ -- `getattr(cls, "codetail")`
        finds nothing and would make this assertion vacuous. Instantiating them
        all is not an option either; most need a matching command object.
        """
        tree = ast.parse(pathlib.Path(anidb_client.responses.__file__).read_text())

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in ("codehead", "codetail", "coderep")
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    offenders.append(f"line {node.lineno}: self.{target.attr} = {node.value.value!r}")

        assert offenders == [], "these need a trailing comma to be tuples: " + "; ".join(offenders)

    def test_banned_reply_parses_without_a_request(self):
        """A ban arrives untagged, so there is no command to resolve it against."""
        resolver = ResponseResolver(b"555 BANNED\n")
        resp = BannedResponse(None, resolver.restag, resolver.rescode, resolver.resstr, resolver.datalines)
        resp.parse()
        assert resp.rescode == "555"

    def test_handle_is_a_no_op_when_there_is_no_request(self):
        """Guards the untagged path: handle() must not dereference a missing command."""
        resolver = ResponseResolver(b"555 BANNED\n")
        resp = BannedResponse(None, resolver.restag, resolver.rescode, resolver.resstr, resolver.datalines)
        resp.parse()
        resp.handle()

    def test_handle_dispatches_to_the_originating_command(self):
        seen = []
        cmd = Command("ANIME", aid=1)
        cmd.callback = seen.append

        resolver = ResponseResolver(b"T001 330 NO SUCH ANIME\n")
        resp = resolver.resolve(cmd)
        resp.parse()
        resp.handle()

        assert seen == [resp]
