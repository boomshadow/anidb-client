"""Tests for Anime.relations and the transitive walk over it.

The walk is the only caller that reads relations off an anime it did not ask
for. Every other read in the library is of an object the caller named, which by
then has a cached row; the walk reaches anime nobody has ever mentioned, and
that is the state in which `relations` answered with the cached row's own
relation objects rather than the (type, Anime) pairs it promises. The caller
unpacking those raised `TypeError: cannot unpack non-iterable
AnimeRelationTable object` -- from the second hop, never the first.

So the fixtures here are deliberately *partially* cached. A graph seeded whole
never reaches the failure: every node already has a row, so nothing is ever
resolved mid-walk. That is why the original report reproduced against the live
service and not offline, and the offline reproduction was the one that lied.

The chain is AniDB's real Maken-ki shape, and it is multi-hop on purpose. Two
seasons of a series are not linked to each other -- the chain threads through
whatever OVAs shipped between them -- so a fixture with a single relation would
pass while exercising none of the traversal:

    8265 (TV, season 1) -> 8566 (OVA) -> 10191 (OVA) -> 9406 (TV, season 2)
"""

import pytest

from tests import factories
from tests.objectlayer import FakeResponse

# The four aids of the chain above, plus a fifth AniDB does not have, for the
# walk that has to stop rather than fail.
TITLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<animetitles>
  <anime aid="8265">
    <title type="main" xml:lang="x-jat">Maken-Ki!</title>
  </anime>
  <anime aid="8566">
    <title type="main" xml:lang="x-jat">Maken-Ki! OVA</title>
  </anime>
  <anime aid="10191">
    <title type="main" xml:lang="x-jat">Maken-Ki! Two OVA</title>
  </anime>
  <anime aid="9406">
    <title type="main" xml:lang="x-jat">Maken-Ki! Two</title>
  </anime>
  <anime aid="99999">
    <title type="main" xml:lang="x-jat">Not In AniDB</title>
  </anime>
</animetitles>
"""

# Relation codes as AniDB words them on the wire (mapper.anime_relation_map).
SEQUEL = "1"
PREQUEL = "2"


def anime_reply(aid, relations=()):
    """A complete ANIME reply, so the anime it describes can be cached as a new row.

    Every non-nullable column of AnimeTable has to arrive here: the callback
    inserts a row built from the reply alone, and a half-filled one fails on the
    commit for reasons that have nothing to do with relations.
    """
    dataline = {
        "aid": str(aid),
        "year": "2011",
        "type": "TV Series",
        "nr_of_episodes": "12",
        "highest_episode_number": "12",
        "special_ep_count": "0",
        "vote_count": "0",
        "temp_vote_count": "0",
        "review_count": "0",
        "is_18_restricted": "0",
        "anidb_updated": "1300000000",
        "special_count": "0",
        "credit_count": "0",
        "other_count": "0",
        "trailer_count": "0",
        "parody_count": "0",
    }
    if relations:
        dataline["related_aid_list"] = "'".join(str(related) for related, _type in relations)
        dataline["related_aid_type"] = "'".join(relation_type for _related, relation_type in relations)
    return FakeResponse("230", datalines=[dataline])


@pytest.fixture
def chain(anidb, session, link, monkeypatch):
    """The Maken-ki chain, cached only at its head.

    8265 has a row and a relation to 8566. Nothing else is cached, so every hop
    after the first has to be fetched during the walk -- which is the condition
    under test, not an incidental detail of the fixture.
    """
    factories.install_title_data(monkeypatch, TITLES_XML)

    head = factories.make_anime(aid=8265)
    session.add(head)
    session.commit()
    session.add(factories.make_relation(anime_pk=head.pk, related_aid=8566))
    session.commit()

    replies = {
        8566: anime_reply(8566, [(8265, PREQUEL), (10191, SEQUEL)]),
        10191: anime_reply(10191, [(8566, PREQUEL), (9406, SEQUEL)]),
        9406: anime_reply(9406, [(10191, PREQUEL)]),
    }
    link.on("ANIME", lambda command: replies.get(int(command.parameters["aid"]), FakeResponse("330")))
    return anidb


class TestWalkingToAnimeTheCacheHasNeverSeen:
    def test_the_whole_chain_is_returned(self, chain, link):
        """The reported crash, and the shape that produces it.

        Two seasons and the two OVAs between them: reaching 9406 from 8265 takes
        three hops, and every one of them lands on an anime with no cached row.
        """
        found = chain.Anime(8265).related_anime(only_in_mylist=False)

        assert [anime.aid for anime in found] == [8265, 8566, 10191, 9406]

    def test_the_walk_did_have_to_fetch(self, chain, link):
        """Pins the condition, not just the result.

        If a later fixture were to seed the whole graph, the test above would go
        on passing while exercising nothing -- the failure only exists on the
        path where an anime is resolved mid-walk.
        """
        chain.Anime(8265).related_anime(only_in_mylist=False)

        assert [command.parameters["aid"] for command in link.requests_for("ANIME")] == ["8566", "10191", "9406"]

    def test_relations_answers_pairs_rather_than_rows(self, chain):
        """The same failure one level down, where it actually lives.

        `related_anime()` only made it visible: it unpacks what `relations`
        returns, so a row arriving where a pair was promised raised there. Read
        directly, the wrong shape came back silently.
        """
        relations = chain.Anime(9406).relations

        assert [(relation_type, related.aid) for relation_type, related in relations] == [("prequel", 10191)]

    def test_an_anime_anidb_does_not_have_says_so(self, chain):
        """Asked, answered "no such anime", and the read raises rather than lying.

        The resolution this property now performs is an ordinary fetch and is
        subject to the ordinary outcome: a 330 marks the object illegal, and
        reading it afterwards raises. An empty relation list would be an answer,
        and there is no anime here to have answered it.
        """
        with pytest.raises(chain.errors.IllegalAnimeObject):
            _ = chain.Anime(99999).relations


class TestAWalkThatCannotContinue:
    def test_it_stops_and_returns_what_it_found(self, anidb, session, link, monkeypatch):
        """An id AniDB does not recognise ends the walk, it does not fail it.

        SPEC-001: the traversal returns what it has rather than raising, so a
        single bad edge does not cost the caller the whole answer.
        """
        factories.install_title_data(monkeypatch, TITLES_XML)
        head = factories.make_anime(aid=8265)
        session.add(head)
        session.commit()
        session.add(factories.make_relation(anime_pk=head.pk, related_aid=99999))
        session.commit()
        link.on("ANIME", FakeResponse("330"))

        anime = anidb.Anime(8265)
        found = anime.related_anime(only_in_mylist=False)

        assert len(found) == 2, "the anime asked about, and the one the walk stopped on"
        assert found[0] is anime
