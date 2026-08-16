"""Tests for AniDBObj.__getattr__ -- the fall-through that answers every
attribute the object layer does not define itself.

Almost every read on an Anime, Episode, File or Group lands here, so its two
mistakes were felt everywhere: a cached value that happened to be falsy was
treated as absent and re-fetched over a rate-limited API, and asking any class
without a `relations` property for its relations recursed until the stack ran
out.
"""

import datetime

import pytest

from anidb_client.db import GroupRelationTable, GroupTable
from tests import factories
from tests.objectlayer import FakeResponse


@pytest.fixture
def cached_file(anidb, session):
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    session.add(factories.make_file(aid=6187, eid=96461, fid=12345))
    session.commit()
    return anidb.File(fid=12345)


@pytest.fixture
def cached_group(anidb, session):
    """A group with one relation, so `relations` has something to return."""
    now = datetime.datetime.now(datetime.UTC)
    group = GroupTable(gid=7091, name="Some Fansubs", short="SF", updated=now, last_update_dice=now)
    session.add(group)
    session.commit()
    session.add(GroupRelationTable(group_pk=group.pk, related_gid=8000, relation_type="participant in"))
    session.commit()
    return anidb.Group(gid=7091)


class TestFalsyCachedValues:
    """`local_vars[name]` was a truth test, so 0, "" and False read as missing.

    The cost is not a wrong answer -- the fall-through reaches db_data and mostly
    gets there in the end -- it is that reaching db_data goes through
    update_if_old(), which can spend a UDP request against a flood limit to be
    told the same zero again.
    """

    def test_a_locally_cached_false_is_returned_without_a_fetch(self, cached_file, link):
        cached_file._is_generic = False
        link.requests.clear()

        assert cached_file.is_generic is False
        assert link.commands() == [], "a cached False must not cost an API call"

    def test_the_false_is_returned_as_itself(self, cached_file, link):
        """Not coerced, and not the row's value read back by coincidence."""
        cached_file._is_generic = False

        assert cached_file.is_generic is False

    def test_a_value_that_really_is_absent_still_falls_through(self, cached_file, link):
        """The guard has to keep distinguishing "None" from "falsy".

        Otherwise the fix would pin every unset attribute to None and stop the
        object layer fetching anything at all.
        """
        cached_file._size = None

        assert cached_file.size == 734003200, "should have come from the cached row"


class TestRelations:
    def test_a_group_returns_its_relations(self, cached_group):
        """Group has no `relations` property, so this is the __getattr__ path.

        It used to read `self.relations` from inside __getattr__, which re-entered
        __getattr__ on the same name. Every Group.relations access was a
        RecursionError; the relationship was never reachable at all.
        """
        relations = cached_group.relations

        assert [r.related_gid for r in relations] == [8000]

    def test_a_class_whose_table_has_no_relations_answers_none(self, anidb, session):
        """EpisodeTable has no relations column, and asking is not an error.

        None matches what the fall-through returns for any other unknown
        attribute, rather than raising where nothing else raises.
        """
        session.add(factories.make_anime(aid=6187))
        session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
        session.commit()

        assert anidb.Episode(anime=6187, epno="5").relations is None

    def test_an_anime_still_uses_its_own_property(self, anidb, session, link):
        """Anime declares `relations`, so ordinary lookup answers with the property.

        Pinned because the fix changes the branch Anime bypasses, and the two
        return different shapes -- (type, Anime) pairs here, rows there. "Never
        sees it" is what this test used to claim, and it was not true: a property
        that raises AttributeError falls through to __getattr__ like any missing
        attribute, which is how the rows escaped. See TestDeclaredProperties.
        """
        anime = factories.make_anime(aid=6187)
        session.add(anime)
        session.commit()
        # 7 rather than an arbitrary id: the property builds an Anime for each
        # related aid, which needs a title in the fixture's title cache.
        session.add(factories.make_relation(anime_pk=anime.pk, related_aid=7))
        session.commit()

        link.on("ANIME", FakeResponse("330"))
        relations = anidb.Anime(6187).relations

        assert [relation_type for relation_type, _anime in relations] == ["sequel"]


class TestDeclaredProperties:
    """The fall-through must not answer for a name the class declares itself.

    Python calls __getattr__ whenever ordinary lookup raises AttributeError, and
    a property whose getter raises one is indistinguishable from an attribute
    that was never there. So a broken property silently became a read of the
    cached row -- a different value, of a different shape, under the same name,
    with no error anywhere near the property at fault. That is one bug per
    property rather than one bug; refusing here retires the category.
    """

    def test_a_property_that_raises_is_refused_rather_than_answered_off_the_row(self, anidb, session, monkeypatch):
        """The exact substitution that produced the reported TypeError.

        The row's `relations` are AnimeRelationTable objects and the property's
        are (type, Anime) pairs. Answering with the former where the latter was
        promised does not fail here -- it fails in the caller, unpacking.
        """
        anime_row = factories.make_anime(aid=6187)
        session.add(anime_row)
        session.commit()
        session.add(factories.make_relation(anime_pk=anime_row.pk, related_aid=7))
        session.commit()
        anime = anidb.Anime(6187)

        def raises_attributeerror(_self):
            raise AttributeError("something inside the getter went wrong")

        monkeypatch.setattr(type(anime), "relations", property(raises_attributeerror))

        with pytest.raises(AttributeError, match="Anime.relations is a property"):
            _ = anime.relations

    def test_an_undeclared_name_still_falls_through_to_the_row(self, cached_file):
        """The guard is a refusal, not a wall.

        Almost every read in the object layer is of a name no class declares, and
        those must go on being answered from the cached row.
        """
        assert cached_file.fid == 12345
