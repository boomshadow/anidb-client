#!/usr/bin/env python
#
# This file is part of anidb-client.
#
# anidb-client is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# anidb-client is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with anidb-client.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import difflib
import gzip
import os
import tempfile
import urllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as etree
from typing import Any

import anidb_client.animeobjs
from anidb_client.errors import AniDBError, AniDBFileError

# One anime's row in the Anime-Lists mapping table: the XML element's own
# attributes, plus a "name" and a "map" of per-source mapping dicts grafted on.
# The values are heterogeneous by construction -- a string id sitting next to a
# list of mapping dicts whose episode maps hold either a target episode or a
# (target, part) pair -- so this is stated as a dict of Any rather than a shape
# that would have to be widened at every leaf.
type AnilistEntry = dict[str, Any]

# What an AniDB episode maps to on the target service: one episode, several when
# AniDB splits what the target treats as one, or a (target episode, part) pair
# when several AniDB episodes share one target episode.
type MappedEpisode = int | list[int] | tuple[str, int]

# (aid, matched titles, score of the best title, the best title itself)
type TitleMatch = tuple[int, list[anidb_client.animeobjs.AnimeTitle], float, str | None]

# Sent when fetching the anime-titles / anime-list XML over HTTPS. AniDB asks
# clients to identify themselves distinctly, so this matches the registered UDP
# client name rather than being a generic default.
_animetitles_useragent = "anidbclientpy"
_animetitles_url = "https://anidb.net/api/anime-titles.xml.gz"
_anime_list_url = "https://github.com/Anime-Lists/anime-lists/raw/master/anime-list.xml"
iso_639_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ISO-639-2_utf-8.txt")
_update_interval = datetime.timedelta(hours=36)

titles: etree.Element | None = None
anilist: dict[str, AnilistEntry] | None = None
languages: dict[str, str] | None = None

_tv_mappings: dict[str, dict[str, str]] = {
    "tvdb": {"id": "tvdbid", "season": "defaulttvdbseason", "offset": "episodeoffset", "map_season": "tvdbseason"},
    "tmdb": {"id": "tmdbtv", "season": "tmdbseason", "offset": "tmdboffset", "map_season": "tmdbseason"},
}


def update_xml(url: str) -> str | None:
    file_name = url.split("/")[-1]
    ext = url.split(".")[-1]
    if os.name == "posix":
        cache_file = os.path.join("/var/tmp", file_name)
    else:
        cache_file = os.path.join(tempfile.gettempdir(), file_name)

    tmp_dir = os.path.dirname(cache_file)
    if not os.access(tmp_dir, os.W_OK):
        raise AniDBError(f"Can't get writeable temp path: {tmp_dir}")

    old_file_exists = os.path.isfile(cache_file)
    if old_file_exists:
        stat = os.stat(cache_file)
        file_moddate = datetime.datetime.fromtimestamp(stat.st_mtime)
        if file_moddate > (datetime.datetime.now() - _update_interval):
            return cache_file

    now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S.%f")
    tmp_file = os.path.join(os.path.dirname(cache_file), f".anidb_client_cache{now}.{ext}")

    try:
        with open(tmp_file, "wb") as f:
            req = urllib.request.Request(url, data=None, headers={"User-Agent": _animetitles_useragent})
            # TimeoutError from here is an OSError, so the existing handler below
            # already treats a stalled fetch the way it treats a failed one: warn,
            # and fall back to the cached copy if there is one.
            res = urllib.request.urlopen(req, timeout=anidb_client.HTTP_TIMEOUT)
            anidb_client.log.info(f"Fetching cache file from {url}")
            f.write(res.read())
    except (OSError, urllib.error.URLError) as err:
        anidb_client.log.error(f"Failed to fetch {url}: {err}")
        anidb_client.log.info(
            "You may be temporarily IP-banned from AniDB; bans are automatically lifted after 24 hours!"
        )
        os.remove(tmp_file)
        if old_file_exists:
            return cache_file
        return None

    if not _verify_xml_file(tmp_file):
        anidb_client.log.error(f"Failed to verify xml file: {tmp_file}")
        return None

    os.rename(tmp_file, cache_file)
    return cache_file


def update_anilist() -> None:
    # These are the global variables we want to update
    # reset them here.
    global anilist
    anilist = {}

    xml_file = update_xml(_anime_list_url)
    if not xml_file and not anilist:
        # Was sys.exit(2). Same reasoning as the transport: a library must not
        # terminate its host process over a failed cache fetch -- which is a
        # routine outcome when AniDB has temporarily IP-banned you.
        raise AniDBFileError("Missing, and unable to fetch, list of anime mappings")
    xml = _read_anidb_xml(xml_file)
    if xml is None:
        raise AniDBFileError("Missing, and unable to fetch, list of anime mappings")

    # Iterate every anime entry in XML; save attributes in the anilist dict.
    for anime in xml.iter("anime"):
        aid = anime.attrib["anidbid"]
        # Copied rather than aliased: what goes into anilist grows a "map" and a
        # "name" that are not strings, and this is the Element's own attribute
        # dict. Nothing reads the element again once the loop has passed it, so
        # the copy is equivalent -- it just stops the graft reaching back.
        a_attrs: AnilistEntry = dict(anime.attrib)
        del a_attrs["anidbid"]

        anilist[aid] = a_attrs
        mappings = anime.find("mapping-list")
        # `is not None`, not a truth test. An Element is falsy when it has no
        # children, so a present-but-empty <mapping-list> read as absent -- and
        # ElementTree has deprecated the truth test outright, which this package's
        # own pytest filterwarnings turns into a test failure the moment anything
        # covers this branch.
        if mappings is not None:
            anilist[aid]["map"] = {}
            for m in mappings.iter("mapping"):
                attrs: AnilistEntry = dict(m.attrib)
                if m.text:
                    attrs["epmap"] = {}
                    episodes = m.text.strip(";").split(";")
                    for e in episodes:
                        (a, t) = e.split("-")
                        attrs["epmap"][a] = t

                    # If multiple anidb episodes are mapped to the same tvdb
                    # episode we need to figure out partnumbers; this is
                    # unfortunately broken for movies because of how anidb adds
                    # parts with episode numbers. When scraping movies the part
                    # should probably be ignored.
                    anidb_eps = sorted(attrs["epmap"].keys(), key=lambda x: int(x))
                    newmap = {}
                    for anidb_ep in anidb_eps:
                        my_epno = attrs["epmap"][anidb_ep]
                        others = [x for x in anidb_eps if attrs["epmap"][x] == my_epno]
                        if len(others) == 1:
                            newmap[anidb_ep] = my_epno
                        else:
                            part = others.index(anidb_ep) + 1
                            newmap[anidb_ep] = (my_epno, part)
                    attrs["epmap"] = newmap

                # File the mapping under the service its own season attribute
                # names -- the same attribute _get_tv_episode() then requires of
                # it, which is why both ends read the key out of one table. The
                # Anime-Lists schema keeps the services apart, one <mapping> per
                # service, but the file is community-maintained and the schema is
                # documentation rather than a validated contract, so an element
                # naming both is filed under both. One naming neither is filed
                # nowhere: it describes no service, and the reader would skip it
                # wherever it landed.
                for source, keys in _tv_mappings.items():
                    anilist[aid]["map"].setdefault(source, [])
                    if keys["map_season"] not in attrs:
                        continue
                    anilist[aid]["map"][source].append(attrs)

        name = anime.find("name")
        anilist[aid]["name"] = name.text if name is not None else None


def update_animetitles() -> None:
    global titles
    xml_file = update_xml(_animetitles_url)
    if not xml_file and not titles:
        raise AniDBFileError("Missing, and unable to fetch, list of anime titles")
    titles = _read_anidb_xml(xml_file)


def _verify_xml_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False

    try:
        tmp_xml = _read_anidb_xml(path)
    except Exception as e:
        anidb_client.log.error(f"Exception when reading xml file: {e}")
        return False

    if tmp_xml is None:
        return False
    # A truncated download still parses, so size is the sanity check.
    return len(tmp_xml.findall("anime")) >= 8000


def _read_anidb_xml(filePath: str | None) -> etree.Element | None:
    return _read_xml_into_etree(filePath)


def _read_xml_into_etree(filePath: str | None) -> etree.Element | None:
    if not filePath:
        return None

    if filePath.split(".")[-1] == "gz":
        with gzip.open(filePath, "rb") as f:
            data = f.read()
    else:
        with open(filePath, "rb") as f:
            data = f.read()

    xmlASetree = etree.fromstring(data)
    return xmlASetree


def _read_language_file() -> None:
    global languages
    languages = {}
    with open(iso_639_file) as f:
        for line in f:
            three, tree2, two, eng, fre = line.strip().split("|")
            if two:
                languages[two] = three


def get_lang_code(short: str | None) -> str | None:
    if not languages:
        _read_language_file()

    # `or {}` rather than a second None check: _read_language_file always leaves a
    # dict behind, and mypy cannot see that it writes the global.
    return (languages or {}).get(short) if short else None


def get_titles(
    name: str | None = None,
    aid: int | None = None,
    max_results: int = 10,
    score_for_match: float = 0.8,
) -> list[TitleMatch]:
    global titles
    res = []

    if titles is None:
        update_animetitles()
    if titles is None:
        raise AniDBFileError("Could not get valid title cache file.")

    for anime in titles.findall("anime"):
        score = 0.0
        best_title_match = None
        exact_match = None
        # `aid` is required by AniDB's own schema for this document, so an entry
        # without one is a corrupt file rather than a case to carry.
        anime_aid = int(anime.attrib["aid"])
        if aid and aid == anime_aid:
            exact_match = str(anime_aid)

        if name:
            name = name.replace("⁄", "/")
            for title in anime.findall("title"):
                title_text = title.text or ""
                if name.lower() in title_text.lower():
                    exact_match = title_text
                diff = difflib.SequenceMatcher(a=name, b=title_text)
                title_score = diff.ratio()
                if title_score > score:
                    score = title_score
                    best_title_match = title_text

        if score > score_for_match or exact_match:
            matched_titles = [
                anidb_client.animeobjs.AnimeTitle(
                    x.get("type"), get_lang_code(x.get("{http://www.w3.org/XML/1998/namespace}lang")), x.text
                )
                for x in anime.findall("title")
            ]
            res.append((anime_aid, matched_titles, score, best_title_match))

    res.sort(key=lambda x: x[2], reverse=True)

    # response is a list of tuples in the form:
    # (<aid>, <list of titles>, <score of best title>, <best title>)
    return res[:max_results]


def anilist_maps(aid: int) -> AnilistEntry:
    global anilist
    if not anilist:
        update_anilist()
    if anilist is None:
        # update_anilist either fills this in or raises; mirrors get_titles above.
        raise AniDBFileError("Could not get valid anime mapping cache file.")
    return anilist.get(str(aid), {})


def _get_tvid(aid: int, key: str) -> str | None:
    maps = anilist_maps(aid)
    if key in maps:
        value: str = maps[key]
        try:
            int(value)
        except ValueError:
            return None
        return value
    return None


def get_tvdbid(aid: int, id_type: str = "tv") -> str | None:
    if id_type == "tv":
        return _get_tvid(aid, "tvdbid")
    return None


def _get_movieid(aid: int, key: str) -> str | list[str] | None:
    maps = anilist_maps(aid)
    if key in maps and maps[key] not in ["", "unknown"]:
        value: str = maps[key]
        if "," in value:
            return value.split(",")
        return value
    return None


def get_tmdbid(aid: int, id_type: str = "movie") -> str | list[str] | None:
    if id_type == "tv":
        return _get_tvid(aid, "tmdbtv")
    elif id_type == "movie":
        return _get_movieid(aid, "tmdbid")
    return None


def get_imdbid(aid: int, id_type: str = "movie") -> str | list[str] | None:
    if id_type == "movie":
        return _get_movieid(aid, "imdbid")
    return None


# return (season, epno) where season is a int
# epno can be:
# An int for an episode number
# A tuple with episode number + partnumber when multiple anidb episodes maps to
# the same target or
# An array with episodes number if the anidb episode is mapped to multiple
# target episodes
def _db_ep_to_resp(db_epno: str | int | tuple[str, int]) -> MappedEpisode | None:
    if type(db_epno) is int:
        return db_epno
    if type(db_epno) is tuple:
        return db_epno
    if type(db_epno) is str:
        eps = [int(x) for x in db_epno.split("+")]
        if len(eps) == 1:
            return eps[0]
        return eps
    return None


def _get_tv_episode(aid: int, epno: str | int, source: str) -> tuple[int | None, MappedEpisode | None]:
    keys = _tv_mappings[source]
    maps = anilist_maps(aid)
    if keys["id"] not in maps:
        return (None, None)

    db_season = maps.get(keys["season"], None)
    if db_season == "a":
        db_season = "1"

    anidb_season = "1"
    anidb_special_offset = 0
    if str(epno).upper().startswith("S"):
        anidb_season = "0"
    elif str(epno).upper().startswith("OP"):
        anidb_season = "0"
        anidb_special_offset = 100
    elif str(epno).upper().startswith("ED"):
        anidb_season = "0"
        anidb_special_offset = 150
    elif str(epno).upper().startswith("T"):
        anidb_season = "0"
        anidb_special_offset = 200
    elif str(epno).upper().startswith("C"):
        anidb_season = "0"
        anidb_special_offset = 300
    elif str(epno).upper().startswith("O"):
        anidb_season = "0"
        anidb_special_offset = 400

    try:
        int_epno = int(str(epno).upper().strip("STOPEDC")) + anidb_special_offset
    except ValueError:
        # Unsupported special type
        return (None, None)

    str_epno = str(int_epno)

    if "map" in maps:
        for m in maps["map"].get(source, []):
            if m["anidbseason"] != anidb_season or keys["map_season"] not in m:
                continue
            if "epmap" in m and str_epno in m["epmap"]:
                # Exact match for episode
                db_epno = m["epmap"][str_epno]
                if db_epno == "0" or type(db_epno) is tuple and db_epno[0] == "0":
                    db_season = None
                    continue
                db_season = m[keys["map_season"]]
                return (int(db_season), _db_ep_to_resp(db_epno))
            if "start" not in m or int_epno < int(m["start"]):
                continue
            if "end" in m and int_epno > int(m["end"]):
                continue
            db_season = m[keys["map_season"]]
            if "offset" in m:
                ret_epno = int(m["offset"]) + int_epno
                if ret_epno < 1:
                    return (None, None)
                return (int(db_season), ret_epno)
    if not db_season:
        # No season specified or episode mapped to 0
        return (None, None)
    if anidb_season == "0":
        # special, but not explicitly mapped in anime-list
        return (0, int_epno)

    if offset := int(maps.get(keys["offset"], 0)):
        ret_epno = offset + int_epno
        if ret_epno < 1:
            return (None, None)
        return (int(db_season), ret_epno)
    return (int(db_season), int_epno)


def get_tv_episode(aid: int, epno: str | int, source: str = "tvdb") -> tuple[int | None, MappedEpisode | None]:
    return _get_tv_episode(aid, epno, source)


def get_tvdb_episode(aid: int, epno: str | int) -> tuple[int | None, MappedEpisode | None]:
    return _get_tv_episode(aid, epno, "tvdb")


def get_tmdb_episode(aid: int, epno: str | int) -> tuple[int | None, MappedEpisode | None]:
    return _get_tv_episode(aid, epno, "tmdb")
