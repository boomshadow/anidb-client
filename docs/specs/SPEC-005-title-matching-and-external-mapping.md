---
title: "Title Matching and External Mapping"
description: "Behavioral expectations for anidb-client's two bulk XML data sources: AniDB's anime-titles export, matched by fuzzy text scoring to turn a title into an anime id, and the Anime-Lists anime-list mapping that translates AniDB anime and episode numbering into TVDB, TMDB and IMDB identifiers — including season and offset mapping, per-episode maps, the special/opening/ending/trailer numbering scheme, movie-part handling, and the fanart.tv lookup that depends on those mappings and an API key."
status: accepted
tags: [titles, anime-titles, fuzzy-matching, difflib, scoring, external-ids, anime-lists, tvdb, tmdb, imdb, mapping, season, episode-offset, epmap, specials, movie-parts, fanart, fanart-tv, api-key, xml, iso-639]
---

# Title Matching and External Mapping

Two bulk XML files, both fetched over HTTPS and cached on disk (SPEC-003), give the library capabilities the UDP API does not offer: turning a human title into an anime id, and translating AniDB's numbering into the numbering used by TVDB, TMDB and IMDB.

## Title matching

AniDB publishes an export of every title it knows — main titles, official titles in various languages, and synonyms. Matching a caller's string against it is fuzzy by necessity: callers type what they remember, and directory names on disk are approximations.

A candidate anime scores against the caller's string on its best-matching title, using sequence similarity. An anime also matches outright when the caller's string appears within one of its titles, or when a requested anime id matches directly. Results come back ranked best-first, and **only the top result becomes an `Anime`** (SPEC-001).

Two properties follow from this that callers need to know:

- **The match threshold is a parameter, not a constant.** Ordinary lookups demand a confident score. Filename-derived guesses (SPEC-004) deliberately demand much less, because the string being matched has been stripped of brackets, episode numbers and punctuation and no longer closely resembles any real title.
- **Ambiguity is real and unresolvable here.** A string that is a synonym of one anime and an official title of another can land on either. A caller that needs certainty passes an anime id.

Titles carry their language as an ISO 639 code, normalised from the two-letter form the export uses to the three-letter form, via a language table shipped with the package.

## External identifier mapping

The Anime-Lists project publishes a community-maintained mapping from AniDB anime to TVDB, TMDB and IMDB. It is the only source of these identifiers — AniDB's own API does not supply them — and its coverage and accuracy are its own.

At the anime level:

- **TVDB** identifiers exist for TV series only.
- **TMDB** exists for both TV and movies, and the two are distinct fields.
- **IMDB** exists for movies only.

Movie identifiers may be a **list** rather than a single value, because one AniDB anime can map to several films. When it does, the anime-level attribute returns the list and the per-episode attributes are the way to get a specific one: the episode's position selects from the list, provided the list length matches the anime's episode count.

### Episode mapping

Translating an AniDB episode number into a season-and-episode pair at TVDB or TMDB is the intricate part, because the two numbering systems disagree in several independent ways.

**Specials are a separate season.** AniDB numbers specials, openings, endings, trailers and credits in their own prefixed sequences; the mapping flattens them into a season-zero numbering by giving each kind a distinct numeric band. A prefixed episode the scheme does not cover has no mapping rather than being forced into one.

**Three mapping mechanisms, in priority order.** An explicit per-episode map wins where one exists for the episode. Failing that, a range with a start, an optional end and an offset applies. Failing that, the anime-level default season and episode offset apply. An episode explicitly mapped to zero is mapped to nothing — that is the mapping's way of saying the episode does not exist at the target.

**An episode can map to several, or to part of one.** When several AniDB episodes map to the same target episode, the result carries a part number alongside the episode number. When one AniDB episode maps to several target episodes, the result is a list. Callers get whichever shape the mapping implies, and the shape is part of the contract.

**Movies are the ragged edge.** AniDB sometimes records the parts of a movie as episodes numbered above one, which the part-numbering machinery then misreads. The library corrects for this where it can — a single-episode anime whose file is not itself a part gets the plain episode, and a higher-numbered episode is treated as a part of the first — and this is documented as imperfect rather than solved.

## Fanart

`Anime.fanart` returns artwork metadata from fanart.tv. It requires both preconditions to be met, and returns an empty list rather than raising when they are not:

- An API key, supplied at init time or found in a netrc file (SPEC-006).
- An external identifier for the anime from the mapping above — fanart.tv is queried by TVDB id for series and by TMDB or IMDB id for movies, so an anime with no mapping cannot be looked up.

The returned structure is translated directly from the fanart.tv API and differs between series and movies; the library does not normalise it, and the fanart.tv reference is the description of its shape. `download_fanart()` fetches the images themselves, at full size or as a low-resolution preview.

A lookup that returns nothing for one identifier does not abandon the rest — the anime may be mapped at several sources. A transport-level failure does end the lookup, returning what has been gathered so far rather than a partial-and-unmarked result.

## Related Artifacts

- **Line of truth (external):** AniDB's anime-titles export, and the Anime-Lists `anime-list.xml` mapping, both of which define their own formats and neither of which this project controls. The fanart.tv API defines the shape of what `Anime.fanart` returns.
- **Related specs:** SPEC-003 (the 36-hour refresh and fallback-to-stale behavior of both XML caches); SPEC-001 (`Anime` construction from a title, and the `extid()` accessor); SPEC-004 (filename-derived matching at a lowered threshold, and the part-vs-episode distinction); SPEC-006 (the fanart API key and its netrc lookup).
- **Tests:** external mapping — seasons, per-episode maps, special numbering and movie parts — in `tests/unit/test_external_mapping.py`; the field converters and bitmask machinery shared with the UDP layer in `tests/unit/test_mapper.py`; enum conversion of unknown codes in `tests/unit/test_enum_converters.py`. The HTTP fetches are bounded and covered in `tests/unit/test_http_timeouts.py`.
