"""Tests for mapping AniDB episodes onto TVDB and TMDB numbering.

AniDB numbers episodes its own way: specials are S1, S2, openings are OP1,
endings ED1, and a series that TVDB splits across seasons is one flat sequence.
Anime-Lists supplies the translation, and this code applies it.

Getting it wrong is quiet and expensive: a media centre files an episode under
the wrong season, or a whole series scrapes as season 1 episode 1. The return
shape is unusually varied for something undocumented by tests -- an int, an
`(episode, part)` tuple, a list of ints, or `(None, None)` -- and the README
promises all of them.

The mapping documents are supplied by the fixture, which stubs the download and
then runs the real `update_anilist()` over them.
"""

import pytest

from tests import factories


class TestUpdateAnilistParsing:
    """The `update_anilist()` parse loop, read at the table it builds.

    Every other test here reaches it through the fixture, which asks about one
    episode at a time; these look at the whole table, because the loop's failures
    have been in what it puts where rather than in any single lookup. It once
    reimplemented an ElementTree truth test -- `if mappings:` is False for an
    element with no children, so a present-but-empty `<mapping-list/>` read as
    absent -- and it once filed every mapping, whatever service it named, under
    `map["tvdb"]`.
    """

    POPULATED = """<anime-list>
      <anime anidbid="6187" tvdbid="79895">
        <mapping-list><mapping tvdbseason="1">;1-1;2-2;</mapping></mapping-list>
        <name>Toradora!</name>
      </anime>
    </anime-list>"""

    EMPTY_MAPPING_LIST = """<anime-list>
      <anime anidbid="1" tvdbid="1">
        <mapping-list/>
        <name>Seikai no Monshou</name>
      </anime>
    </anime-list>"""

    TMDB_ONLY = """<anime-list>
      <anime anidbid="6187" tmdbtv="79895">
        <mapping-list><mapping anidbseason="1" tmdbseason="1">;1-5;</mapping></mapping-list>
        <name>Toradora!</name>
      </anime>
    </anime-list>"""

    BOTH_SERVICES = """<anime-list>
      <anime anidbid="6187" tvdbid="79895" tmdbtv="46225">
        <mapping-list>
          <mapping anidbseason="1" tvdbseason="1">;1-1;</mapping>
          <mapping anidbseason="1" tmdbseason="2">;1-5;</mapping>
        </mapping-list>
        <name>Toradora!</name>
      </anime>
    </anime-list>"""

    NO_SERVICE = """<anime-list>
      <anime anidbid="6187" tvdbid="79895">
        <mapping-list><mapping anidbseason="1">;1-5;</mapping></mapping-list>
        <name>Toradora!</name>
      </anime>
    </anime-list>"""

    @staticmethod
    def _parse(monkeypatch, xml_text):
        return factories.install_anime_list(monkeypatch, xml_text)

    def test_a_populated_mapping_list_is_parsed(self, monkeypatch):
        anilist = self._parse(monkeypatch, self.POPULATED)

        assert anilist["6187"]["name"] == "Toradora!"
        assert anilist["6187"]["map"]["tvdb"][0]["epmap"] == {"1": "1", "2": "2"}

    def test_a_present_but_empty_mapping_list_is_still_a_mapping_list(self, monkeypatch):
        """The case the truth test got wrong: an Element with no children is
        falsy, so this anime came out with no `map` key at all."""
        anilist = self._parse(monkeypatch, self.EMPTY_MAPPING_LIST)

        assert anilist["1"]["map"] == {}

    def test_a_tmdb_mapping_is_filed_under_tmdb(self, monkeypatch):
        """The loop used to append to whichever source its inner loop ended on,
        which was always `tvdb`. A TMDB-only document therefore built an empty
        `map["tmdb"]` and a `map["tvdb"]` holding a mapping the TVDB reader then
        skipped -- so the per-episode maps, the highest-priority of the three
        mechanisms, were unreachable for TMDB and lookups fell through to the
        anime-level default season silently."""
        anilist = self._parse(monkeypatch, self.TMDB_ONLY)

        assert anilist["6187"]["map"]["tvdb"] == []
        assert anilist["6187"]["map"]["tmdb"][0]["epmap"] == {"1": "5"}

    def test_each_service_gets_only_the_mappings_that_name_it(self, monkeypatch):
        """Anime-Lists writes one `<mapping>` per service, so a document mapped at
        both carries two -- and neither belongs in the other's list."""
        anilist = self._parse(monkeypatch, self.BOTH_SERVICES)
        maps = anilist["6187"]["map"]

        assert [m["tvdbseason"] for m in maps["tvdb"]] == ["1"]
        assert [m["tmdbseason"] for m in maps["tmdb"]] == ["2"]

    def test_a_mapping_naming_no_service_is_filed_nowhere(self, monkeypatch):
        """It describes no service, so there is no list it belongs in -- and the
        reader requires the season attribute it lacks, so it would be skipped
        wherever it landed. It used to land in `map["tvdb"]` by accident."""
        anilist = self._parse(monkeypatch, self.NO_SERVICE)

        assert anilist["6187"]["map"] == {"tvdb": [], "tmdb": []}


@pytest.fixture
def mapped(anidb, session):
    """Two anime: one with a plain season mapping, one with a per-episode map."""
    session.add(factories.make_anime(aid=6187, nr_of_episodes=50))
    session.add(factories.make_anime(aid=1, nr_of_episodes=13))
    session.commit()
    return anidb


def episode(anidb, session, aid, epno, eid=900000, **kwargs):
    session.add(factories.make_episode(aid=aid, eid=eid, epno=epno, **kwargs))
    session.commit()
    return anidb.Episode(eid=eid)


class TestSeasonMapping:
    def test_a_regular_episode_maps_to_the_default_season(self, mapped, session):
        """defaulttvdbseason=1 with no offset: the numbering passes through."""
        ep = episode(mapped, session, aid=6187, epno="5")
        assert ep.tvdb_episode == (1, 5)

    def test_an_unmapped_anime_maps_to_nothing(self, mapped, session):
        """`(None, None)`, not an exception. Most anime have no TVDB entry, and a
        caller has to be able to ask without guarding every call."""
        session.add(factories.make_anime(aid=7, nr_of_episodes=1))
        session.commit()
        ep = episode(mapped, session, aid=7, epno="1", eid=900007)
        assert ep.tvdb_episode == (None, None)


class TestPerEpisodeMaps:
    def test_an_explicitly_mapped_episode_uses_its_mapping(self, mapped, session):
        """Anime-Lists can map episode by episode when the seasons do not line up."""
        ep = episode(mapped, session, aid=1, epno="2", eid=900101)
        assert ep.tvdb_episode == (1, 2)

    def test_two_anidb_episodes_mapping_to_one_tvdb_episode_become_parts(self, mapped, session):
        """AniDB splits some episodes that TVDB keeps whole.

        Episodes 3 and 4 both map to TVDB episode 3, so they come back as
        (episode, part) -- the tuple shape the README documents.
        """
        third = episode(mapped, session, aid=1, epno="3", eid=900103)
        fourth = episode(mapped, session, aid=1, epno="4", eid=900104)

        assert third.tvdb_episode == (1, ("3", 1))
        assert fourth.tvdb_episode == (1, ("3", 2))


class TestSpecialNumbering:
    @pytest.mark.parametrize(
        ("epno", "description"),
        [
            ("S1", "special"),
            ("C1", "credit"),
            ("T1", "trailer"),
            ("O1", "other"),
        ],
    )
    def test_specials_map_into_season_zero(self, mapped, session, epno, description):
        """Every special type belongs to AniDB season 0.

        Without a season-0 mapping in Anime-Lists there is nothing to map to, so
        the answer is (None, None) rather than a wrong season.
        """
        ep = episode(mapped, session, aid=6187, epno=epno, eid=900200 + hash(epno) % 100, type="special")
        season, _number = ep.tvdb_episode
        assert season in (None, 0), f"{description} must not land in a regular season"

    def test_an_unparseable_episode_number_maps_to_nothing(self, mapped, session):
        """AniDB occasionally has episode numbers this scheme cannot express.

        The strip-and-int step raises ValueError on those, and the answer is
        (None, None) -- not a crash in the middle of a library scan.
        """
        ep = episode(mapped, session, aid=6187, epno="XYZ", eid=900300, type="other")
        assert ep.tvdb_episode == (None, None)


class TestTmdbUsesTheSameMachinery:
    def test_tmdb_mapping_is_absent_when_the_document_has_none(self, mapped, session):
        """This anime is mapped at tvdb only, so tmdb must decline rather than
        reuse it.

        Worth pinning: both sources go through one function parameterised by a
        key table, and the failure mode of getting that wrong is returning TVDB
        numbering as though it were TMDB.
        """
        ep = episode(mapped, session, aid=6187, epno="5", eid=900400)
        assert ep.tmdb_episode == (None, None)

    def test_a_per_episode_tmdb_map_is_used(self, mapped, session):
        """The end of the path the misfiling broke.

        Anime 1 is mapped at both services, to a different season and episode at
        each: tvdb says season 1 episode 1, tmdb says season 2 episode 5. While
        every mapping was filed under tvdb this answered from the anime-level
        fallback instead -- a different answer, arrived at silently.
        """
        ep = episode(mapped, session, aid=1, epno="1", eid=900401)

        assert ep.tmdb_episode == (2, 5)
        assert ep.tvdb_episode == (1, 1)


class TestMoviePartHandling:
    @pytest.mark.parametrize(("epno", "expected_part"), [("3", 2), ("4", 3)])
    def test_a_single_episode_anime_treats_extra_episodes_as_parts(self, anidb, session, epno, expected_part):
        """AniDB files the parts of a split movie as episodes 2, 3, ...

        A media centre wants one movie, so for an anime with one episode, AniDB
        episode N is re-read as part N-1 of the mapped episode rather than as an
        Nth episode.
        """
        session.add(factories.make_anime(aid=1, nr_of_episodes=1))
        session.commit()

        ep = episode(anidb, session, aid=1, epno=epno, eid=900500 + int(epno))
        assert ep.tvdb_episode == (1, ("3", expected_part))

    def test_episode_one_of_a_single_episode_anime_has_no_part(self, anidb, session):
        """Part numbering starts at the second episode; the first is the whole thing.

        Note the type: a singly-mapped episode comes back as an **int**, while the
        part tuples above carry a **string** episode number. That inconsistency is
        inherited, and it is recorded here rather than smoothed over -- callers
        comparing `tvdb_episode[1]` against a number will match one shape and miss
        the other, and normalising it now would change what existing callers see.
        """
        session.add(factories.make_anime(aid=1, nr_of_episodes=1))
        session.commit()

        ep = episode(anidb, session, aid=1, epno="1", eid=900510)
        assert ep.tvdb_episode == (1, 1)
