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
import enum
from collections.abc import Callable, Container, Mapping, Sequence
from typing import Any

import anidb_client
from anidb_client.db import (
    AnimeRelationType,
    EpisodeType,
    GroupRelationType,
    MylistFileState,
    MylistState,
)

# One field of an AniDB reply, as a string, turned into whatever the cache stores
# for it. The return is deliberately Any: the whole point of a converter table is
# that each entry produces a different type -- an int, a date, a bool, a member of
# a vocabulary -- and the caller writes the result straight into a column.
type FieldConverter = Callable[[str], Any]

_blacklist = ("unused", "retired", "reserved", "not_implemented")


def _enum_converter[T: enum.StrEnum](table: Mapping[str, T], field: str) -> Callable[[str | None], T | None]:
    """Convert an AniDB enumeration code, tolerating codes this table does not list.

    Every converter built here runs inside a response callback, on the response
    thread, and those callbacks set the event a caller is blocked on only after the
    whole conversion loop has finished. So a KeyError raised in here is not one
    dropped field -- it escapes the callback, the event is never set, and the
    calling application waits forever.

    AniDB extends these enumerations without announcing it, which makes an
    unrecognised code an expected event rather than a corrupt one. It degrades to
    None, and says so once, rather than deadlocking the caller.

    Admitting a genuinely new code therefore starts in db.py, not here: add the
    member to the vocabulary, which is the schema change, and then point a wire
    code at it in the table below. There is no way round that ordering -- these
    tables hold members, so naming one that does not exist fails at import.
    """

    def convert(value: str | None) -> T | None:
        if not value:
            return None
        try:
            return table[value]
        except KeyError:
            anidb_client.log.warning(f"Unknown AniDB {field} code {value!r}; leaving the field unset")
            return None

    return convert


# each line is one byte
# only change this if the api changes
anime_map_a_converters: dict[str, FieldConverter] = {
    "aid": int,
    "nr_of_episodes": int,
    "highest_episode_number": int,
    "special_ep_count": int,
    "air_date": lambda x: datetime.date(1970, 1, 1) + datetime.timedelta(seconds=int(x)) if x and int(x) else None,
    "end_date": lambda x: datetime.date(1970, 1, 1) + datetime.timedelta(seconds=int(x)) if x and int(x) else None,
    "rating": lambda x: int(x) / 100 if x else None,
    "vote_count": int,
    "temp_rating": lambda x: int(x) / 100 if x else None,
    "temp_vote_count": int,
    "average_review_rating": lambda x: int(x) / 100 if x else None,
    "review_count": int,
    "is_18_restricted": lambda x: x == "1",
    "ann_id": int,
    "allcinema_id": int,
    "anidb_updated": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "special_count": int,
    "credit_count": int,
    "other_count": int,
    "trailer_count": int,
    "parody_count": int,
}

# AniDB's wire codes on the left, the schema's vocabulary on the right. The
# right-hand side is db.py's -- these tables select from a vocabulary rather than
# restating one, which is what keeps the two files from drifting apart.
mylist_state_map = {
    "0": MylistState.UNKNOWN,
    "1": MylistState.ON_HDD,
    "2": MylistState.ON_CD,
    "3": MylistState.DELETED,
}

mylist_filestate_map = {
    "0": MylistFileState.NORMAL_ORIGINAL,
    "1": MylistFileState.CORRUPTED,
    "2": MylistFileState.SELF_EDITED,
    "10": MylistFileState.SELF_RIPPED,
    "11": MylistFileState.ON_DVD,
    "12": MylistFileState.ON_VHS,
    "13": MylistFileState.ON_TV,
    "14": MylistFileState.IN_THEATERS,
    "15": MylistFileState.STREAMED,
    "100": MylistFileState.OTHER,
}

file_map_f_converters: dict[str, FieldConverter] = {
    "fid": int,
    "aid": int,
    "eid": int,
    "lid": int,
    "gid": int,
    "is_deprecated": lambda x: x == "1",
    "size": int,
    "ed2khash": lambda x: x or None,
    "length_in_seconds": int,
    "description": lambda x: x or None,
    "aired_date": lambda x: datetime.date(1970, 1, 1) + datetime.timedelta(seconds=int(x)) if x and int(x) else None,
    "mylist_state": _enum_converter(mylist_state_map, "mylist state"),
    "mylist_filestate": _enum_converter(mylist_filestate_map, "mylist file state"),
    "mylist_viewed": lambda x: x == "1",
    "mylist_viewdate": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "mylist_storage": lambda x: x or None,
    "mylist_source": lambda x: x or None,
    "mylist_other": lambda x: x or None,
}

episode_type_map = {
    "1": EpisodeType.REGULAR,
    "2": EpisodeType.SPECIAL,
    "3": EpisodeType.CREDIT,
    "4": EpisodeType.TRAILER,
    "5": EpisodeType.PARODY,
    "6": EpisodeType.OTHER,
}

episode_map_converters: dict[str, FieldConverter] = {
    "eid": int,
    "aid": int,
    "length": int,
    "rating": lambda x: int(x) / 100 if x else None,
    "votes": int,
    "aired": lambda x: datetime.date(1970, 1, 1) + datetime.timedelta(seconds=int(x)) if x and int(x) else None,
    "type": _enum_converter(episode_type_map, "episode type"),
}

mylist_map_converters: dict[str, FieldConverter] = {
    "lid": int,
    "fid": int,
    "eid": int,
    "aid": int,
    "gid": int,
    "mylist_state": _enum_converter(mylist_state_map, "mylist state"),
    # MYLIST returns the file state as its last field. It has to be converted here
    # as well as parsed in responses.py: the callback converts every key the reply
    # carried, so a field named in the response with no entry in this table raises
    # KeyError -- which, on the response thread, is a hang rather than an error.
    "mylist_filestate": _enum_converter(mylist_filestate_map, "mylist file state"),
    "mylist_viewdate": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "mylist_storage": lambda x: x or None,
    "mylist_source": lambda x: x or None,
    "mylist_other": lambda x: x or None,
}

# Several wire codes collapse onto one vocabulary entry -- AniDB distinguishes
# shades of "alternative setting" that the schema does not.
anime_relation_map = {
    "1": AnimeRelationType.SEQUEL,
    "2": AnimeRelationType.PREQUEL,
    "11": AnimeRelationType.SAME_SETTING,
    "12": AnimeRelationType.ALTERNATIVE_SETTING,
    "21": AnimeRelationType.ALTERNATIVE_SETTING,
    "22": AnimeRelationType.ALTERNATIVE_SETTING,
    "31": AnimeRelationType.ALTERNATIVE_VERSION,
    "32": AnimeRelationType.ALTERNATIVE_VERSION,
    "41": AnimeRelationType.MUSIC_VIDEO,
    "42": AnimeRelationType.CHARACTER,
    "51": AnimeRelationType.SIDE_STORY,
    "52": AnimeRelationType.PARENT_STORY,
    "61": AnimeRelationType.SUMMARY,
    "62": AnimeRelationType.FULL_STORY,
    "100": AnimeRelationType.OTHER,
}

group_map_converters: dict[str, FieldConverter] = {
    "gid": int,
    "rating": int,
    "votes": int,
    "acount": int,
    "fcount": int,
    "founded": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "disbanded": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "dateflags": int,
    "last_release": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
    "last_activity": lambda x: datetime.datetime.fromtimestamp(int(x)) if x and int(x) else None,
}

# Codes 1-6 are the relation as seen from this group; 101-106 are the same
# relations seen from the other end, which is why each has an inverse here.
group_relation_map = {
    "1": GroupRelationType.PARTICIPANT_IN,
    "2": GroupRelationType.PARENT_OF,
    "3": GroupRelationType.LOST_PART,
    "4": GroupRelationType.MERGED_FROM,
    "5": GroupRelationType.NOW_KNOWN_AS,
    "6": GroupRelationType.OTHER,
    "101": GroupRelationType.INCLUDES,
    "102": GroupRelationType.CHILD_OF,
    "103": GroupRelationType.SPLIT_FROM,
    "104": GroupRelationType.MERGED_INTO,
    "105": GroupRelationType.FORMERLY,
    "106": GroupRelationType.OTHER,
}

roman_numbering: dict[str, int] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
    "xvi": 16,
    "xvii": 17,
    "xviii": 18,
    "xix": 19,
    "xx": 20,
    "xxi": 21,
    "xxii": 22,
    "xxiii": 23,
    "xxiv": 24,
    "xxv": 25,
    "xxvi": 26,
    "xxvii": 27,
    "xxviii": 28,
    "xxix": 29,
    "xxx": 30,
    # If you got this far you should really consider the regular numbering
    # system..
}

anime_map_a: list[str] = [
    "aid",
    "unused",
    "year",
    "type",
    "related_aid_list",
    "related_aid_type",
    "retired",
    "retired",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "retired",
    "retired",
    "nr_of_episodes",
    "highest_episode_number",
    "special_ep_count",
    "air_date",
    "end_date",
    "url",
    "picname",
    "retired",
    "rating",
    "vote_count",
    "temp_rating",
    "temp_vote_count",
    "average_review_rating",
    "review_count",
    "not_implemented",
    "is_18_restricted",
    "retired",
    "ann_id",
    "allcinema_id",
    "animenfo_id",
    "unused",
    "unused",
    "unused",
    "anidb_updated",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "unused",
    "unused",
    "unused",
    "unused",
    "special_count",
    "credit_count",
    "other_count",
    "trailer_count",
    "parody_count",
    "unused",
    "unused",
    "unused",
]

file_map_f: list[str] = [
    "unused",
    "aid",
    "eid",
    "gid",
    "lid",
    "not_implemented",
    "is_deprecated",
    "state",
    "size",
    "ed2khash",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "unused",
    "unused",
    "reserved",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "not_implemented",
    "length_in_seconds",
    "description",
    "aired_date",
    "unused",
    "unused",
    "not_implemented",
    "mylist_state",
    "mylist_filestate",
    "mylist_viewed",
    "mylist_viewdate",
    "mylist_storage",
    "mylist_source",
    "mylist_other",
    "unused",
]
# Trailing entries retained from the upstream map, commented out:
# seven 'not_implemented' slots followed by 'unused'.

file_map_a: list[str] = [
    "anime_total_episodes",
    "highest_episode_number",
    "year",
    "type",
    "related_aid_list",
    "related_aid_type",
    "category_list",
    "reserved",
    "romaji_name",
    "kanji_name",
    "english_name",
    "other_name",
    "short_name_list",
    "synonym_list",
    "retired",
    "retired",
    "epno",
    "ep_name",
    "ep_romaji_name",
    "ep_kanji_name",
    "episode_rating",
    "episode_vote_count",
    "unused",
    "unused",
    "group_name",
    "group_short_name",
    "unused",
    "unused",
    "unused",
    "unused",
    "unused",
    "date_aid_record_updated",
]


def getAnimeBitsA(amask: Container[str]) -> str:
    bitmap = anime_map_a
    return _getBitChain(bitmap, amask)


def getAnimeCodesA(aBitChain: str) -> list[str]:
    amap = anime_map_a
    return _getCodes(amap, aBitChain)


def getFileBitsF(fmask: Container[str]) -> str:
    fmap = file_map_f
    return _getBitChain(fmap, fmask)


def getFileCodesF(bitChainF: str) -> list[str]:
    fmap = file_map_f
    return _getCodes(fmap, bitChainF)


def getFileBitsA(amask: Container[str]) -> str:
    amap = file_map_a
    return _getBitChain(amap, amask)


def getFileCodesA(bitChainA: str) -> list[str]:
    amap = file_map_a
    return _getCodes(amap, bitChainA)


def _getBitChain(attrmap: Sequence[str], wanted: Container[str]) -> str:
    """Return an hex string with the correct bit set corresponding to the wanted fields in the map"""
    bit = 0
    for index, field in enumerate(attrmap):
        if field in wanted and field not in _blacklist:
            # OR, not XOR. Each position is visited once, so the two agreed here,
            # but XOR says "toggle" where the intent is "set" -- and it would have
            # cleared a bit rather than setting it had a name ever repeated.
            bit |= 1 << (len(attrmap) - index - 1)

    # Zero-padded to one hex digit per four fields. Formatted directly rather than
    # via hex() + lstrip("0x"): lstrip removes any leading "0" and "x" characters,
    # not the prefix, so it also ate the leading zeros of masks like 0x0f -- which
    # only came out right because the manual re-pad put them back. (The .rstrip("L")
    # that followed it stripped the suffix Python 2 put on long integers.)
    return f"{bit:0{len(attrmap) // 4}x}"


def _getCodes(attrmap: Sequence[str], bitChain: str) -> list[str]:
    """Returns a list with the corresponding fields as set in the bitChain (hex string)"""
    bits = int(bitChain, 16)
    mapLength = len(attrmap)
    # Walking the map forwards and the bits downwards is the same traversal the
    # index arithmetic used to spell out, and yields the fields in map order.
    return [field for index, field in enumerate(attrmap) if bits & (1 << (mapLength - index - 1))]
