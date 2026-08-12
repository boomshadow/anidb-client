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

The relation table is the same vocabulary reached by a different route: the ANIME
callback looks it up itself rather than through a converter, so it needed the same
tolerance separately. TestUnknownAnimeRelationCodes covers that end of it.
"""

import datetime
import logging

import pytest

from anidb_client import db, mapper
from tests import factories
from tests.objectlayer import FakeResponse


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


class TestUnknownAnimeRelationCodes:
    """A relation AniDB names in a way we cannot must not take the caller with it.

    The ANIME callback built its relation rows with `anime_relation_map[code]` and
    `int(aid)`, both unguarded, and `_updated.set()` is its last statement -- so a
    relation code added to AniDB after this table was written did not lose one edge,
    it blocked the calling application permanently with nothing in the log. Reaching
    it takes no more than asking for an anime that has such a relation.

    Each test refreshes a cached anime and then reads its relations back, so the
    assertion also carries the invariant: `update()` waits on the event the callback
    sets last, and only returns if the callback finished. The suite-wide
    pytest-timeout is the backstop against the regression, which is a hang rather
    than an error.
    """

    @pytest.fixture
    def refreshed(self, anidb, link, session):
        """Refresh a cached anime against an ANIME reply carrying relation lists.

        Seeded rather than fetched from nothing: the reply below carries only the
        relation fields, and AnimeTable has a dozen non-nullable columns, so a fresh
        insert would fail on the commit for reasons that have nothing to do with
        relations. Merging into an existing row keeps the test on its subject.
        """

        def refresh(related_aid_list, related_aid_type):
            session.add(factories.make_anime(aid=6187))
            session.commit()
            link.on(
                "ANIME",
                FakeResponse(
                    "230",
                    datalines=[
                        {
                            "aid": "6187",
                            "related_aid_list": related_aid_list,
                            "related_aid_type": related_aid_type,
                        }
                    ],
                ),
            )
            anime = anidb.Anime(6187)
            anime.update(block=True)
            return anime

        return refresh

    def test_an_unknown_relation_code_does_not_hang_the_caller(self, refreshed):
        """Where the hang actually lands, and why this reads an ordinary attribute.

        The response runs on its own thread, so the exception did not surface to the
        caller -- it ended that thread, which left `_updating` held and `_updated`
        clear. `relations` is a property and reaches neither, but every ordinary
        attribute goes through `__getattr__`, which acquires that lock first. So the
        anime answers nothing, ever, from the next read onwards. This test fails by
        timing out rather than by erroring; the suite-wide timeout is what ends it.
        """
        anime = refreshed(related_aid_list="7", related_aid_type="9999")

        assert anime._updated.is_set(), "the response thread must always release its waiter"
        assert anime.year == "2009"
        assert anime.relations == []

    def test_the_relations_either_side_of_a_bad_one_still_land(self, refreshed):
        """Dropping the pair must not become dropping the batch.

        1 and 7 rather than arbitrary ids: the `relations` property builds an Anime
        per related aid, so each needs a title in the fixture's title cache. The
        dropped pair names one of those too, so a regression here reads as an extra
        entry rather than as an unrelated title lookup failing.
        """
        relations = refreshed(related_aid_list="1'6187'7", related_aid_type="1'9999'2").relations

        assert [(rtype, related.aid) for rtype, related in relations] == [("sequel", 1), ("prequel", 7)]

    def test_a_non_numeric_related_aid_is_skipped_the_same_way(self, refreshed):
        """`int(aid)` is the other half of the same line, and fails the same way."""
        relations = refreshed(related_aid_list="not-an-aid'7", related_aid_type="1'2").relations

        assert [(rtype, related.aid) for rtype, related in relations] == [("prequel", 7)]

    def test_the_warning_names_the_offending_code(self, refreshed, caplog):
        """The only signal a maintainer gets that the table has fallen behind.

        AniDB does not publish the numeric relation codes anywhere machine-readable,
        so there is no way to check this table for completeness ahead of time -- the
        log line is how a missing entry is ever discovered.
        """
        with caplog.at_level("WARNING", logger="anidb_client.test"):
            refreshed(related_aid_list="7", related_aid_type="9999")

        assert "9999" in caplog.text

    def test_a_known_relation_is_still_stored(self, refreshed):
        """The fix must not have made every relation unstorable."""
        relations = refreshed(related_aid_list="7", related_aid_type="1").relations

        assert [(rtype, related.aid) for rtype, related in relations] == [("sequel", 7)]


class TestUnknownGroupRelationCodes:
    """The group half of the same vocabulary, which had the same hang.

    A GROUP reply words its relations as "gid,code" strings rather than as two
    parallel lists, but `group_relation_map[code]` was looked up with no default in
    a callback whose last statement releases the caller -- so an unlisted group
    relation code blocked the caller exactly as an unlisted anime one did. Group
    has no `relations` property, so these reads go through `__getattr__`, which
    acquires the update lock the crashed response thread never released: the hang
    lands on the assertion itself, and the suite-wide timeout is what ends it.
    """

    @pytest.fixture
    def refreshed(self, anidb, link, session):
        """Refresh a cached group against a GROUP reply carrying relations."""

        def refresh(relations):
            now = datetime.datetime.now(datetime.UTC)
            session.add(db.GroupTable(gid=7091, name="Some Fansubs", short="SF", updated=now, last_update_dice=now))
            session.commit()
            link.on("GROUP", FakeResponse("250", datalines=[{"relations": relations}]))
            group = anidb.Group(gid=7091)
            group.update(block=True)
            return group

        return refresh

    def test_an_unknown_relation_code_does_not_hang_the_caller(self, refreshed):
        group = refreshed("8000,9999")

        assert group._updated.is_set(), "the response thread must always release its waiter"
        assert group.relations == []

    def test_the_relations_either_side_of_a_bad_one_still_land(self, refreshed):
        relations = refreshed("8000,1'8001,9999'8002,2").relations

        assert [(r.related_gid, r.relation_type) for r in relations] == [
            (8000, "participant in"),
            (8002, "parent of"),
        ]

    def test_a_non_numeric_related_gid_is_skipped(self, refreshed):
        """Not a hang on its own, but it loses the whole group row rather than one row.

        A non-numeric gid reaches a non-nullable integer column and fails the
        commit, which is caught and logged -- so every other field the reply carried
        is rolled back with it. Dropped by the same rule as an unreadable code.
        """
        relations = refreshed("not-a-gid,1'8002,2").relations

        assert [(r.related_gid, r.relation_type) for r in relations] == [(8002, "parent of")]

    def test_the_warning_names_the_offending_code(self, refreshed, caplog):
        with caplog.at_level("WARNING", logger="anidb_client.test"):
            refreshed("8000,9999")

        assert "9999" in caplog.text

    def test_a_related_gid_is_stored_as_a_number(self, refreshed, session):
        """It was stored as the string AniDB sent, which made the reconcile miss.

        The relation rows are reconciled by comparing `related_gid` against the
        cached row's, and the cached side is an integer column -- so a freshly built
        row carrying "8000" never matched the 8000 already there, and a refresh
        replaced the relation rather than updating it.
        """
        group = refreshed("8000,1")

        assert [r.related_gid for r in group.relations] == [8000]
