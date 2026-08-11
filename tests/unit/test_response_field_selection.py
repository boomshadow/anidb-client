"""Tests for the response classes that decide their own field layout.

Of the 106 classes in responses.py, 100 are purely declarative -- an __init__ that
assigns three tuples of field names. Their whole failure mode is already covered
by the ast sweep in test_responses.py.

Six contain actual logic, choosing which fields a reply carries based on what the
request asked for. Those are the ones worth testing individually, and two of them
were wrong.

Getting this wrong is not a parse error. The reply is positional, so a wrong
field list shifts every value into the neighbouring column, or drops one off the
end and leaves the caller reading a KeyError.
"""

from anidb_client.commands import AnimeCommand, AuthCommand, FileCommand, NotifyCommand
from anidb_client.mapper import file_map_f, getFileBitsA, getFileBitsF
from anidb_client.responses import ResponseResolver


class TestLoginAcceptedNewVersion:
    """Code 201: login succeeded, but this client version is outdated.

    Dormant until AniDB decides a registered client version is old -- which
    happens the day a newer version of this client is registered. On that day
    every deployed copy of the older one takes this path.
    """

    def test_the_nat_address_is_parsed_when_nat_was_requested(self):
        """Regression: the nat check evaluated to false for every input.

        It read `int(nat is None and nat or "0")`, which returns 0 for 1, "1",
        None and 0 alike -- the `is None` is inverted, and the `and`/`or` chain
        then collapses to "0" regardless. So `address` was never added to the
        field list.

        The consequence is a hang, not an error. link.py treats 201 as a
        successful login, then _auth_handler reads attrs["address"], raises
        KeyError on the response thread, and never sets the authenticated event
        -- so every command queued behind the login blocks forever.
        """
        cmd = AuthCommand("user", "pw", 3, "anidbclientpy", 1, nat=1)
        resp = ResponseResolver(b"T001 201 sess1234 203.0.113.7:41234 LOGIN ACCEPTED - NEW VERSION AVAILABLE\n")
        parsed = resp.resolve(cmd)
        parsed.parse()

        assert parsed.attrs["sesskey"] == "sess1234"
        assert parsed.attrs["address"] == "203.0.113.7:41234"

    def test_no_address_is_parsed_when_nat_was_not_requested(self):
        cmd = AuthCommand("user", "pw", 3, "anidbclientpy", 1)
        resp = ResponseResolver(b"T001 201 sess1234 LOGIN ACCEPTED - NEW VERSION AVAILABLE\n")
        parsed = resp.resolve(cmd)
        parsed.parse()

        assert parsed.attrs == {"sesskey": "sess1234"}

    def test_it_matches_the_ordinary_login_response(self):
        """200 and 201 differ only in whether a new version exists.

        They were written separately and drifted; the field layout must not
        depend on which of the two AniDB happens to send.
        """
        accepted = ResponseResolver(b"T001 200 sess 203.0.113.7:41234 LOGIN ACCEPTED\n")
        new_ver = ResponseResolver(b"T001 201 sess 203.0.113.7:41234 LOGIN ACCEPTED - NEW VERSION AVAILABLE\n")

        for resolver in (accepted, new_ver):
            parsed = resolver.resolve(AuthCommand("u", "p", 3, "c", 1, nat=1))
            parsed.parse()
            assert set(parsed.attrs) == {"sesskey", "address"}


class TestNotification:
    def test_a_buddy_username_does_not_raise(self):
        """Regression: `int(buddy is not None and buddy or "0")` on a username.

        NotifyCommand takes a buddy *name*, so the expression reached
        int("someuser") and raised ValueError while building the response object
        -- on the response thread, where it would leave the caller waiting.
        """
        cmd = NotifyCommand(buddy="someuser")
        resolver = ResponseResolver(b"T001 290 NOTIFICATION\n1|2|3\n")
        parsed = resolver.resolve(cmd)
        parsed.parse()

        assert parsed.datalines[0] == {"notifies": "1", "msgs": "2", "buddys": "3"}

    def test_without_a_buddy_only_two_counts_are_read(self):
        cmd = NotifyCommand()
        resolver = ResponseResolver(b"T001 290 NOTIFICATION\n1|2\n")
        parsed = resolver.resolve(cmd)
        parsed.parse()

        assert parsed.datalines[0] == {"notifies": "1", "msgs": "2"}


class TestFileFieldsFollowTheRequestedMasks:
    """FileResponse builds its field list from the fmask and amask that were sent.

    Same seam as AnimeResponse: the request's bitmask is the only thing naming
    the reply's columns, so the two have to agree exactly.
    """

    def test_the_column_names_come_from_the_masks(self):
        fmask = getFileBitsF(["aid", "eid", "size", "ed2khash"])
        amask = getFileBitsA(["epno"])
        cmd = FileCommand(fid=1, fmask=fmask, amask=amask)

        resolver = ResponseResolver(b"T001 220 FILE\n1|6187|96461|734003200|abc123|5\n")
        parsed = resolver.resolve(cmd)
        parsed.parse()

        line = parsed.datalines[0]
        assert line["fid"] == "1"
        assert line["aid"] == "6187"
        assert line["eid"] == "96461"
        assert line["size"] == "734003200"
        assert line["ed2khash"] == "abc123"
        assert line["epno"] == "5"

    def test_fid_is_always_the_first_column(self):
        """It is prepended to the mask-derived list rather than selected by it."""
        cmd = FileCommand(fid=1, fmask=getFileBitsF(file_map_f), amask=getFileBitsA(["epno"]))
        resolver = ResponseResolver(b"T001 220 FILE\n42\n")
        parsed = resolver.resolve(cmd)
        parsed.parse()

        assert parsed.datalines[0]["fid"] == "42"


class TestAnimeFieldsFollowTheRequestedMask:
    def test_an_empty_mask_names_no_columns(self):
        """A degenerate request should produce an empty mapping, not a crash."""
        from anidb_client.mapper import getAnimeBitsA

        cmd = AnimeCommand(aid=1, amask=getAnimeBitsA([]))
        resolver = ResponseResolver(b"T001 230 ANIME\n\n")
        parsed = resolver.resolve(cmd)
        parsed.parse()

        assert parsed.datalines == [{}]


class TestEveryMappedCodeCanBeConstructed:
    """Build one of every response class in the table.

    100 of them are declarative and this is close to free; the point is that a
    class whose __init__ raises would otherwise only be discovered when AniDB
    first sent that code -- on the response thread, where an exception means the
    caller waits forever rather than sees an error.
    """

    def test_no_response_class_raises_on_construction(self):
        from anidb_client.responses import responses

        # A command carrying every parameter these classes read from it.
        cmd = FileCommand(fid=1, fmask=getFileBitsF(["aid"]), amask=getFileBitsA(["epno"]))
        cmd.parameters.setdefault("nat", 1)
        cmd.parameters.setdefault("buddy", "someuser")
        from anidb_client.mapper import getAnimeBitsA

        cmd.parameters["amask"] = getAnimeBitsA(["aid"])

        failures = []
        for code, cls in sorted(responses.items()):
            try:
                cls(cmd, "T001", code, "SOME TEXT", [])
            except Exception as exc:  # noqa: BLE001 - reporting, not handling
                failures.append(f"{code} {cls.__name__}: {type(exc).__name__}: {exc}")

        assert failures == [], "; ".join(failures)
