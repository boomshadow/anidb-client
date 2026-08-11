"""Builders for the data the library normally downloads or caches.

The object layer reads three things: the SQL cache, the anime-titles XML, and the
Anime-Lists mapping XML. Supply all three and `Anime`, `Episode` and `File`
construct and answer entirely offline, using their real code paths -- no patching
of the classes under test.

That is deliberate. `AniDBObj` overrides both `__getattribute__` and `__getattr__`,
so a partially-built instance recurses rather than working; objects have to be
created the way the library creates them.
"""

import datetime
import xml.etree.ElementTree as etree

from anidb_client.db import AnimeRelationTable, AnimeTable, EpisodeTable, FileTable

UTC = datetime.UTC

# Two series and a movie, enough to exercise title matching, relations and the
# tvdb/tmdb mappings without carrying a copy of AniDB's real 60 MB title list.
ANIME_TITLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<animetitles>
  <anime aid="6187">
    <title type="main" xml:lang="x-jat">Kemono no Souja Erin</title>
    <title type="official" xml:lang="en">Erin</title>
    <title type="synonym" xml:lang="en">Beast Player Erin</title>
  </anime>
  <anime aid="1">
    <title type="main" xml:lang="x-jat">Seikai no Monshou</title>
    <title type="official" xml:lang="en">Crest of the Stars</title>
  </anime>
  <anime aid="7">
    <title type="main" xml:lang="x-jat">Kidou Senshi Gundam</title>
    <title type="official" xml:lang="en">Mobile Suit Gundam</title>
  </anime>
</animetitles>
"""

# Anime-Lists mapping: one series with a straightforward tvdb season mapping, and
# one with a per-episode map including a two-parter.
ANIME_LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<anime-list>
  <anime anidbid="6187" tvdbid="83243" defaulttvdbseason="1" episodeoffset="0">
    <name>Kemono no Souja Erin</name>
  </anime>
  <anime anidbid="1" tvdbid="70863" defaulttvdbseason="1" episodeoffset="0">
    <name>Seikai no Monshou</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="1">;1-1;2-2;3-3;4-3;</mapping>
    </mapping-list>
  </anime>
</anime-list>
"""


def install_title_data(monkeypatch, titles_xml=ANIME_TITLES_XML):
    """Put a parsed anime-titles document where anames expects to find it.

    Without this, get_titles() calls update_animetitles() and fetches from AniDB --
    which the network guard blocks, loudly.
    """
    import anidb_client.anames

    monkeypatch.setattr(anidb_client.anames, "titles", etree.fromstring(titles_xml))


def install_anime_list(monkeypatch, anime_list_xml=ANIME_LIST_XML):
    """Populate the tvdb/tmdb mapping table that update_anilist() would download."""
    import anidb_client.anames

    monkeypatch.setattr(anidb_client.anames, "anilist", None)
    xml = etree.fromstring(anime_list_xml)

    anilist = {}
    for anime in xml.iter("anime"):
        attrs = dict(anime.attrib)
        aid = attrs.pop("anidbid")
        anilist[aid] = attrs
        mappings = anime.find("mapping-list")
        if mappings is not None:
            anilist[aid]["map"] = {"tvdb": [], "tmdb": []}
            for m in mappings.iter("mapping"):
                entry = dict(m.attrib)
                if m.text:
                    epmap = {}
                    for pair in m.text.strip(";").split(";"):
                        anidb_ep, tvdb_ep = pair.split("-")
                        epmap[anidb_ep] = tvdb_ep
                    # Two AniDB episodes mapping to one TVDB episode become
                    # (episode, part) tuples, mirroring update_anilist().
                    ordered = sorted(epmap, key=int)
                    resolved = {}
                    for anidb_ep in ordered:
                        target = epmap[anidb_ep]
                        siblings = [x for x in ordered if epmap[x] == target]
                        if len(siblings) == 1:
                            resolved[anidb_ep] = target
                        else:
                            resolved[anidb_ep] = (target, siblings.index(anidb_ep) + 1)
                    entry["epmap"] = resolved
                anilist[aid]["map"]["tvdb"].append(entry)
        name = anime.find("name")
        anilist[aid]["name"] = name.text if name is not None else None

    monkeypatch.setattr(anidb_client.anames, "anilist", anilist)
    return anilist


def _stamp(updated=None, dice=None):
    now = datetime.datetime.now(UTC)
    return updated or now, dice or now


def make_anime(aid=6187, updated=None, last_update_dice=None, anidb_updated=None, **overrides):
    """An AnimeTable row with every non-nullable column filled in.

    `updated` and `last_update_dice` are the cache-freshness clock; pass them
    explicitly to test refresh behaviour rather than freezing time.
    """
    updated, dice = _stamp(updated, last_update_dice)
    row = {
        "aid": aid,
        "year": "2009",
        "type": "TV Series",
        "nr_of_episodes": 50,
        "highest_episode_number": 50,
        "special_ep_count": 0,
        "vote_count": 0,
        "temp_vote_count": 0,
        "review_count": 0,
        "is_18_restricted": False,
        "anidb_updated": anidb_updated or datetime.datetime.now(),
        "special_count": 0,
        "credit_count": 4,
        "other_count": 0,
        "trailer_count": 0,
        "parody_count": 0,
        "updated": updated,
        "last_update_dice": dice,
    }
    row.update(overrides)
    return AnimeTable(**row)


def make_episode(aid=6187, eid=96461, epno="5", updated=None, last_update_dice=None, **overrides):
    updated, dice = _stamp(updated, last_update_dice)
    row = {
        "aid": aid,
        "eid": eid,
        "epno": epno,
        "length": 25,
        "votes": 0,
        "title_eng": "Erin and the Egg Thieves",
        "type": "regular",
        "updated": updated,
        "last_update_dice": dice,
    }
    row.update(overrides)
    return EpisodeTable(**row)


def make_file(aid=6187, eid=96461, fid=12345, updated=None, last_update_dice=None, **overrides):
    updated, dice = _stamp(updated, last_update_dice)
    row = {
        "aid": aid,
        "eid": eid,
        "fid": fid,
        "is_generic": False,
        "size": 734003200,
        "ed2khash": "d41d8cd98f00b204e9800998ecf8427e",
        "updated": updated,
        "last_update_dice": dice,
    }
    row.update(overrides)
    return FileTable(**row)


def make_relation(anime_pk, related_aid, relation_type="sequel"):
    return AnimeRelationTable(anime_pk=anime_pk, related_aid=related_aid, relation_type=relation_type)
