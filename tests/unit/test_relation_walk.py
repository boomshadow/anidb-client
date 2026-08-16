"""Tests for Anime.relations and the transitive walk over it.

The walk is the only caller that reads relations off an anime it did not ask
for. Every other read in the library is of an object the caller named, which by
then has a cached row; the walk reaches anime nobody has ever mentioned, and
that is the state in which `relations` answered with the cached row's own
relation objects rather than the (type, Anime) pairs it promises. The caller
unpacking those raised `TypeError: cannot unpack non-iterable
AnimeRelationTable object` -- from the second hop, never the first.

So the fixtures here are deliberately *partially* cached. A graph seeded whole
never reaches that failure: every node already has a row, so nothing is ever
resolved mid-walk. That is why the original report reproduced against the live
service and not offline, and the offline reproduction was the one that lied.

Both fixtures are AniDB's real shapes, because both make a point the walk has to
get right (SPEC-010 carries the measurements):

- `chain` is multi-hop. Two seasons of a series are not linked to each other --
  the chain threads through whatever OVAs shipped between them -- so a fixture
  with a single relation would pass while exercising none of the traversal.
- `franchise` sprawls through `other`, which is the edge that reaches a
  franchise ancestor and, through it, decades of series. It is what the
  traversal budget exists to survive.
"""

import inspect

import pytest

from anidb_client.animeobjs import DEFAULT_RELATION_BUDGET, Anime, RelationWalkStop
from tests import factories
from tests.objectlayer import FakeResponse

TITLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<animetitles>
  <anime aid="8265"><title type="main" xml:lang="x-jat">Maken-Ki!</title></anime>
  <anime aid="8566"><title type="main" xml:lang="x-jat">Maken-Ki! OVA</title></anime>
  <anime aid="10191"><title type="main" xml:lang="x-jat">Maken-Ki! Two OVA</title></anime>
  <anime aid="9406"><title type="main" xml:lang="x-jat">Maken-Ki! Two</title></anime>
  <anime aid="11372"><title type="main" xml:lang="x-jat">Iron-Blooded Orphans</title></anime>
  <anime aid="12048"><title type="main" xml:lang="x-jat">Iron-Blooded Orphans 2nd Season</title></anime>
  <anime aid="14587"><title type="main" xml:lang="x-jat">Iron-Blooded Orphans - Urdr Hunt</title></anime>
  <anime aid="715"><title type="main" xml:lang="x-jat">Kidou Senshi Gundam</title></anime>
  <anime aid="716"><title type="main" xml:lang="x-jat">Kidou Senshi Z Gundam</title></anime>
  <anime aid="717"><title type="main" xml:lang="x-jat">Kidou Senshi Gundam ZZ</title></anime>
  <anime aid="718"><title type="main" xml:lang="x-jat">Kidou Senshi Gundam F91</title></anime>
  <anime aid="99999"><title type="main" xml:lang="x-jat">Not In AniDB</title></anime>
</animetitles>
"""

# Relation codes as AniDB words them on the wire (mapper.anime_relation_map).
SEQUEL = "1"
PREQUEL = "2"
SIDE_STORY = "51"
PARENT_STORY = "52"
OTHER = "100"


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


def seed_head(session, aid, relations):
    """Cache one anime and its relation rows, and nothing else.

    Everything the walk reaches beyond this has to be resolved during the walk,
    which is the condition under test rather than an incidental detail.
    """
    head = factories.make_anime(aid=aid)
    session.add(head)
    session.commit()
    for related_aid, relation_type in relations:
        session.add(factories.make_relation(anime_pk=head.pk, related_aid=related_aid, relation_type=relation_type))
    session.commit()


def script(link, replies):
    link.on("ANIME", lambda command: replies.get(int(command.parameters["aid"]), FakeResponse("330")))


@pytest.fixture
def chain(anidb, session, link, monkeypatch):
    """8265 (TV) -> 8566 (OVA) -> 10191 (OVA) -> 9406 (TV), cached only at its head."""
    factories.install_title_data(monkeypatch, TITLES_XML)
    seed_head(session, 8265, [(8566, "sequel")])
    script(
        link,
        {
            8566: anime_reply(8566, [(8265, PREQUEL), (10191, SEQUEL)]),
            10191: anime_reply(10191, [(8566, PREQUEL), (9406, SEQUEL)]),
            9406: anime_reply(9406, [(10191, PREQUEL)]),
        },
    )
    return anidb


@pytest.fixture
def franchise(anidb, session, link, monkeypatch):
    """A show, its own entries, and the `other` edge out to the franchise.

    11372's own season two and side story sit behind story relations; 715 is the
    1979 series, a different show by any reading, and everything past it is the
    sprawl an unbounded walk inherits.
    """
    factories.install_title_data(monkeypatch, TITLES_XML)
    seed_head(session, 11372, [(12048, "sequel"), (14587, "side story"), (715, "other")])
    script(
        link,
        {
            12048: anime_reply(12048, [(11372, PREQUEL)]),
            14587: anime_reply(14587, [(11372, PARENT_STORY)]),
            715: anime_reply(715, [(716, OTHER)]),
            716: anime_reply(716, [(717, OTHER)]),
            717: anime_reply(717, [(718, OTHER)]),
            718: anime_reply(718, []),
        },
    )
    return anidb


def aids(result):
    return [anime.aid for _relation_type, anime in result.related]


class TestWalkingToAnimeTheCacheHasNeverSeen:
    def test_the_whole_chain_is_returned(self, chain):
        """The reported crash, and the shape that produces it.

        Two seasons and the two OVAs between them: reaching 9406 from 8265 takes
        three hops, and every one of them lands on an anime with no cached row.
        """
        result = chain.Anime(8265).related_anime()

        assert result.root.aid == 8265
        assert aids(result) == [8566, 10191, 9406]

    def test_the_walk_did_have_to_fetch(self, chain, link):
        """Pins the condition, not just the result.

        If a later fixture were to seed the whole graph, the test above would go
        on passing while exercising nothing -- the failure only exists on the
        path where an anime is resolved mid-walk.
        """
        chain.Anime(8265).related_anime()

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

        Resolving an uncached anime is an ordinary fetch and takes the ordinary
        outcome: a 330 marks the object illegal, and reading it afterwards
        raises. An empty relation list would be an answer, and there is no anime
        here to have answered it.
        """
        with pytest.raises(chain.errors.IllegalAnimeObject):
            _ = chain.Anime(99999).relations


class TestTheRelationTypeComesBack:
    """The type is discovered during the walk and cannot be recovered after it.

    A caller given a bare set of anime cannot tell a sequel from a franchise
    link, and re-deriving one means walking the graph again over a rate-limited
    API to learn what the first walk already knew.
    """

    def test_every_anime_arrives_with_the_edge_it_was_reached_by(self, franchise):
        result = franchise.Anime(11372).related_anime()

        assert [(relation_type, anime.aid) for relation_type, anime in result.related] == [
            ("sequel", 12048),
            ("side story", 14587),
            ("other", 715),
            ("other", 716),
            ("other", 717),
            ("other", 718),
        ]

    def test_the_type_is_the_last_edge_not_the_route(self, chain):
        """9406 is three hops out and reports the third hop, not the first.

        Stated because it is the one thing about this return that a reader can
        get wrong in a way nothing catches: two anime reached `sequel -> sequel`
        and `other -> sequel` arrive looking identical.
        """
        result = chain.Anime(8265).related_anime()

        assert result.related[-1][0] == "sequel"


class TestBoundingTheWalk:
    def test_relation_types_the_caller_did_not_name_are_not_followed(self, franchise):
        """The caller's policy, applied where the caller states one.

        715 is reachable only by `other`, so naming the story types leaves the
        1979 series and everything behind it out of the answer entirely.
        """
        result = franchise.Anime(11372).related_anime(follow=("sequel", "prequel", "side story", "parent story"))

        assert aids(result) == [12048, 14587]
        assert not result.truncated

    def test_a_type_the_caller_did_not_name_is_not_traversed_through(self, franchise, link):
        """Not returned is not enough: it must not be walked either.

        An excluded edge that was still followed would spend the requests the
        filter was set to avoid, and would reach the sprawl anyway.
        """
        franchise.Anime(11372).related_anime(follow=("sequel",))

        assert [command.parameters["aid"] for command in link.requests_for("ANIME")] == ["12048"]

    def test_depth_stops_the_walk_at_a_distance(self, franchise):
        result = franchise.Anime(11372).related_anime(depth=1)

        assert aids(result) == [12048, 14587, 715]

    def test_the_budget_stops_the_walk_at_a_count(self, franchise):
        result = franchise.Anime(11372).related_anime(budget=3)

        assert aids(result) == [12048, 14587, 715]

    def test_excluded_anime_are_walls(self, franchise):
        """Neither returned nor traversed through, which is not the same rule."""
        result = franchise.Anime(11372).related_anime(exclude=[franchise.Anime(715)])

        assert aids(result) == [12048, 14587]

    def test_the_walk_follows_anime_outside_mylist_by_default(self, chain):
        """The default that made the function inert for anyone not using mylist.

        Nothing here is in a mylist, and this is the whole answer rather than
        nothing at all.
        """
        assert aids(chain.Anime(8265).related_anime()) == [8566, 10191, 9406]

    def test_mylist_membership_still_bounds_a_walk_that_asks_for_it(self, chain, session):
        """Retained as a use-case filter for someone cataloguing a collection.

        10191 is not in mylist, so it is not followed, and 9406 behind it is
        never reached.
        """
        session.add(factories.make_file(aid=8566, eid=1, fid=1, lid=1))
        session.commit()

        assert aids(chain.Anime(8265).related_anime(only_in_mylist=True)) == [8566]


class TestSayingWhichBoundEndedTheWalk:
    """Nine because that is all there are, and nine because the ceiling was hit,
    must not look identical to a caller. An answer shaped like a complete one
    that is not is worse than an error: the caller acts on it and has no reason
    to doubt it.
    """

    def test_a_walk_that_ran_out_of_graph_is_not_marked_truncated(self, chain):
        result = chain.Anime(8265).related_anime()

        assert result.stopped_by is None
        assert not result.truncated

    def test_a_walk_that_spent_its_budget_exactly_is_still_complete(self, franchise):
        """The off-by-one that would make every full walk look truncated.

        Six anime and a budget of six: the budget is spent, and nothing was left
        behind, so there is nothing to report.
        """
        result = franchise.Anime(11372).related_anime(budget=6)

        assert len(result.related) == 6
        assert not result.truncated

    def test_hitting_the_budget_is_reported(self, franchise):
        result = franchise.Anime(11372).related_anime(budget=3)

        assert result.stopped_by == RelationWalkStop.BUDGET
        assert result.truncated

    def test_hitting_the_depth_limit_is_reported(self, franchise):
        """Reported whether or not anything lay beyond it.

        Checking would mean reading the relations of every anime on the
        boundary, which costs exactly the requests the bound was set to prevent.
        """
        result = franchise.Anime(11372).related_anime(depth=1)

        assert result.stopped_by == RelationWalkStop.DEPTH

    def test_stopping_on_an_id_anidb_does_not_recognise_is_reported(self, anidb, session, link, monkeypatch):
        """The walk stops rather than failing -- and says that it stopped.

        Returning the partial set silently is the same defect as truncating
        silently: the caller cannot tell it from the whole answer.
        """
        factories.install_title_data(monkeypatch, TITLES_XML)
        seed_head(session, 8265, [(99999, "sequel")])
        script(link, {})

        result = anidb.Anime(8265).related_anime()

        assert result.stopped_by == RelationWalkStop.UNKNOWN_ANIME
        assert result.related == [], "an id AniDB does not have is not an anime to return"


class TestTheDefaultBound:
    def test_a_walk_is_bounded_unless_the_caller_says_otherwise(self):
        """The default is the safety property, so it is pinned rather than assumed.

        An unbounded default on a franchise-scale component is a large number of
        rate-limited lookups, which is how a client earns a ban.
        """
        budget = inspect.signature(Anime.related_anime).parameters["budget"]

        assert budget.default == DEFAULT_RELATION_BUDGET
        assert isinstance(DEFAULT_RELATION_BUDGET, int)
