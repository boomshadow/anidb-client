"""An AniDB enumeration code we do not recognise must not deadlock the caller.

The converters in mapper.py run inside a response callback, on the response
thread, and every one of those callbacks sets the event a caller is blocked on
only after the whole conversion loop has finished. So a KeyError raised by a
converter is not one dropped field. It escapes the callback, the event is never
set, and the application waits forever with no indication why -- the same shape
as the 330 and login hangs already fixed here.

This is not hypothetical: AniDB extends these enumerations without announcing it.
mylist state 4 ("remote storage") is one such addition, and is deliberately still
absent from the table -- the matching column in db.py constrains the same set, so
admitting a new value needs a schema change alongside. Until then it has to
degrade rather than hang, which is what these pin.
"""

import logging

import pytest

from anidb_client import db, mapper


@pytest.fixture
def log(monkeypatch):
    """mapper logs through the library logger, which is None until init()."""
    import anidb_client

    logger = logging.getLogger("anidb_client.test")
    monkeypatch.setattr(anidb_client, "log", logger, raising=False)
    return logger


CONVERTERS = [
    ("file_map_f_converters", "mylist_state"),
    ("file_map_f_converters", "mylist_filestate"),
    ("mylist_map_converters", "mylist_state"),
    ("mylist_map_converters", "mylist_filestate"),
    ("episode_map_converters", "type"),
]


@pytest.mark.parametrize(("table", "field"), CONVERTERS)
def test_an_unknown_code_converts_to_none_instead_of_raising(table, field, log):
    convert = getattr(mapper, table)[field]

    assert convert("9999") is None


@pytest.mark.parametrize(("table", "field"), CONVERTERS)
def test_an_unknown_code_is_reported(table, field, log, caplog):
    """Degrading quietly would turn a hang into silent data loss."""
    convert = getattr(mapper, table)[field]

    with caplog.at_level("WARNING", logger="anidb_client.test"):
        convert("9999")

    assert "9999" in caplog.text


@pytest.mark.parametrize(("table", "field"), CONVERTERS)
def test_an_unknown_code_does_not_raise_before_init_has_set_a_logger(table, field, monkeypatch):
    """The warning path must not itself be the thing that raises.

    `log` is None until init() runs. Nothing should reach a response converter
    before then, but a guard that only works after initialisation is not a guard.
    """
    import anidb_client

    monkeypatch.setattr(anidb_client, "log", None, raising=False)
    convert = getattr(mapper, table)[field]

    assert convert("9999") is None


class TestKnownCodesAreUnaffected:
    """The fix must not have made every lookup return None."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [("0", "unknown"), ("1", "on hdd"), ("2", "on cd"), ("3", "deleted")],
    )
    def test_each_mylist_state_still_converts(self, code, expected, log):
        assert mapper.mylist_map_converters["mylist_state"](code) == expected

    @pytest.mark.parametrize(
        ("code", "expected"),
        [("0", "normal/original"), ("11", "on dvd"), ("100", "other")],
    )
    def test_each_mylist_filestate_still_converts(self, code, expected, log):
        assert mapper.file_map_f_converters["mylist_filestate"](code) == expected

    @pytest.mark.parametrize(
        ("code", "expected"),
        [("1", "regular"), ("2", "special"), ("6", "other")],
    )
    def test_each_episode_type_still_converts(self, code, expected, log):
        assert mapper.episode_map_converters["type"](code) == expected

    @pytest.mark.parametrize(("table", "field"), CONVERTERS)
    def test_an_empty_value_is_still_none(self, table, field, log):
        """Absent and unrecognised both mean "nothing to store", as before."""
        convert = getattr(mapper, table)[field]

        assert convert("") is None
        assert convert(None) is None


VOCABULARIES = [
    ("mylist_state_map", db.MylistState),
    ("mylist_filestate_map", db.MylistFileState),
    ("episode_type_map", db.EpisodeType),
    ("anime_relation_map", db.AnimeRelationType),
    ("group_relation_map", db.GroupRelationType),
]


@pytest.mark.parametrize(("table", "vocabulary"), VOCABULARIES, ids=[t for t, _ in VOCABULARIES])
class TestTheWireTablesSelectFromTheSchemaVocabulary:
    """The coupling this suite exists to enforce, now enforced by construction.

    These five vocabularies used to be written out twice -- here as the values of
    mapper's conversion tables, and again as bare strings in db.py's Enum()
    columns. Nothing tied them together, so adding a value to one and not the
    other produced a row the database rejects or a wire code converting to a
    string no column accepts, and only at runtime.

    Now mapper selects from the schema's enums rather than restating them. These
    two assertions are cheap and they are what fails if someone puts a bare string
    back into a table.
    """

    def test_every_mapped_value_is_a_member(self, table, vocabulary):
        assert all(isinstance(v, vocabulary) for v in getattr(mapper, table).values())

    def test_every_member_is_reachable_from_some_wire_code(self, vocabulary, table):
        """A vocabulary entry no code maps to is dead -- either a typo or a gap."""
        assert set(getattr(mapper, table).values()) == set(vocabulary)


def test_every_field_named_in_a_mylist_reply_has_a_converter():
    """The coupling that makes adding a response field dangerous.

    `_anidb_mylist_data_callback` converts every key the reply carried, looking
    each one up in mylist_map_converters without a default. So naming a field in
    MylistResponse.codetail without adding a converter for it produces a KeyError
    on the response thread -- a hang, not a missing field. `date` is the standing
    exception: the callback deletes it before the loop.
    """
    from anidb_client.responses import MylistResponse

    # Read straight off the class. This used to need __new__ plus a hand-called
    # __init__, because codetail was only assigned on the instance.
    missing = [f for f in MylistResponse.codetail if f != "date" and f not in mapper.mylist_map_converters]

    assert not missing, f"MYLIST fields with no converter, which would hang the caller: {missing}"
