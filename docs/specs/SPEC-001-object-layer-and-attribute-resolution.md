---
title: "Object Layer and Attribute Resolution"
description: "Behavioral expectations for the anidb-client object layer: how Anime, Episode, File and Group objects are constructed from a title, an id or a path, how reading an attribute lazily resolves from cache and only then from the AniDB API, how an object AniDB does not recognise becomes an illegal object rather than hanging its caller, the update() forced refresh, transitive relation walking, and the equality and containment semantics the objects offer."
status: accepted
tags: [object-layer, anime, episode, file, group, attribute-resolution, lazy-loading, illegal-object, relations, related-anime, update, equality, containment, public-api]
---

# Object Layer and Attribute Resolution

This is the surface callers actually touch. `Anime`, `Episode`, `File` and `Group` are ordinary Python objects whose attributes appear to be plain data, while behind each read sits a cache lookup and — sometimes — a rate-limited call to an API that bans clients for asking too often. This spec describes what a caller can expect from that arrangement.

## Construction

An object is constructed from whatever the caller happens to know, and construction resolves identity immediately rather than deferring it:

- **`Anime`** takes a title or an anime id. A title is resolved against the anime-titles data (SPEC-005) by fuzzy matching, and only the single best match becomes the object. Titles are genuinely ambiguous — a search that matches a synonym of one anime and an official title of another can land on either — so a caller who needs a specific anime should pass the id.
- **`Episode`** takes an anime plus an episode number, or an episode id alone. The anime may be given as a title, an id, or an `Anime`; whichever form arrives, the episode's `anime` attribute is an `Anime` object.
- **`File`** takes a local path, a file id, a mylist id, or an anime plus an episode. Path-based construction is the primary use and is covered in SPEC-004.
- **`Group`** takes a group name (short or long) or a group id.

Construction that cannot establish an identity at all raises rather than producing a half-built object: an `Anime` whose title matches nothing, an `Episode` given neither an episode id nor an anime-plus-number, a `File` given none of its four accepted forms, a `Group` given neither name nor id.

Immediately after identity is established, the object loads whatever the local cache already holds for it. A cache hit means subsequent attribute reads answer without any network call at all.

## Attribute resolution

Reading an attribute resolves in this order, stopping at the first that can answer:

1. A value the object already holds in memory — including one that is legitimately falsy. Zero votes, an empty description and `False` are answers, not absences, and must not be mistaken for "not fetched yet".
2. The cached row, if the object has one and it is fresh enough. Freshness is SPEC-003's subject.
3. A fetch from AniDB, after which the cached row is updated and the value read from it.

An attribute that is unknown to the object after all three answers `None` rather than raising. This is deliberate: the AniDB API returns different field sets under different conditions, and a caller reading a field the API did not supply gets an absence, not an error.

Concurrent reads of the same object do not produce concurrent fetches. A read arriving while a fetch is already in flight waits for that fetch and then reads its result.

### Forced refresh

`update()` bypasses freshness entirely and re-fetches from AniDB. It is the escape hatch for a caller who knows the cached data is stale — the library's own policy is deliberately conservative about spending network calls, and this is how a caller overrides it.

## Objects AniDB does not recognise

An id or title that AniDB has no record of produces an **illegal object**. Once an object is marked illegal, reading any attribute on it raises `IllegalAnimeObject` instead of answering.

The critical property is that becoming illegal must never make an object unable to report it. A fetch that discovers the object is not real still completes — it marks the object illegal, releases whatever the caller is waiting on, and lets the next attribute read raise. An object that cannot say it is invalid is a permanently blocked caller, which is strictly worse than an exception.

## Relations

`Anime.relations` is a list of `(relation_type, Anime)` pairs — sequels, prequels, side stories and the rest — built from the relation rows cached alongside the anime.

`Anime.related_anime()` walks those links transitively and returns the connected set, starting with the anime itself. Two controls bound the walk:

- **`exclude`** is an iterable of `Anime` treated as walls: neither returned nor traversed through.
- **`only_in_mylist`**, set by default, follows only anime already in the caller's mylist. Without it a single sequel link drags in an entire franchise.

A relation AniDB describes in terms this library cannot name is **dropped, and logged** — a relation type absent from the wire table, or a related id that is not a number, costs that one pair and leaves the rest of the relations intact. The kinds are a constrained vocabulary in the cache (SPEC-003), so an unrecognised one can be stored neither as itself nor under a stand-in that would later be indistinguishable from the real thing; the edge is lost until the table learns the code, and the log line is the only signal that it has fallen behind. What this must not do is fail the fetch: the rows are built on the response thread, where an exception is not a missing field but a permanently blocked caller. Group-to-group relations are read the same way, from a reply that words them differently but constrains them just as narrowly, and are dropped by the same rule.

The relation graph is cyclic by construction — every sequel link has a matching prequel link back — so the walk terminates by refusing to revisit anime it has already found or queued, not by depth. If the walk encounters an anime AniDB does not recognise, it stops there and returns what it has found so far rather than failing the whole traversal.

An anime is "in mylist" when the cache holds at least one file for it that carries a mylist id. This is answered from the cache alone; it never triggers a fetch.

## Equality and containment

The objects implement the comparisons that make collection use natural, and each compares on identity rather than on cached field values:

- Two `Anime` are equal when their anime ids match. `episode in anime` is true when the episode belongs to that anime.
- Two `Episode` are equal by episode id, falling back to mylist id and then to episode number when one side has not resolved an id yet.
- Two `File` are equal when their file ids match; two generic files (SPEC-004) are equal when they cover the same episode. Two distinct real files are never equal — including when neither has a file id.
- `len(file)` is the number of episodes the file covers, and `episode in file` tests membership in that set. The comparison is against episode numbers as text (SPEC-004), which is the only reason it can answer at all — a set built in some other representation would compare unequal to every episode and report false without erroring.

**Equality** against an unrelated type returns `NotImplemented`, so Python falls back to identity comparison rather than the object claiming an answer it does not have.

**Containment** must not do the same, and the distinction is easy to get wrong. `in` has no reflected form to fall back to: it coerces whatever `__contains__` returns straight to a bool, and `NotImplemented` is not a legal answer there — Python 3.14 raises `TypeError` on that coercion rather than quietly treating it as true. A containment test against an unrelated type is therefore simply **false**.

## Images

`download_image()` writes the AniDB cover image for an `Anime` or a `Group` to a caller-supplied file handle, and raises when the object type has no image concept or the object has no picture recorded. It reaches the AniDB CDN over HTTPS, which is a different path from the UDP transport and is not subject to its pacing.

Fanart is a separate source with separate preconditions; see SPEC-005.

## Related Artifacts

- **Line of truth (self-enforcing):** `src/anidb_client/db.py` — the cache schema whose rows back every attribute read. `anidb_client.__all__` in `src/anidb_client/__init__.py` — the package's declared public surface.
- **Related specs:** SPEC-003 (when a cached value counts as fresh, and what the cache stores); SPEC-002 (the transport that a fetch goes out over, and its pacing); SPEC-004 (`File` construction from a path, and mylist operations); SPEC-005 (title matching, external ids and fanart); SPEC-006 (`init()`, which must run before any object is constructed).
- **Tests:** the object layer is exercised through the fixture in `tests/objectlayer.py` against the fake server in `tests/fake_anidb.py`. Attribute-resolution behavior lives in `tests/unit/test_attribute_resolution.py` (falsy cached values, relation reads), illegal-object and not-found paths in `tests/unit/test_notfound_paths.py`, and the fixture's own guarantees in `tests/unit/test_objectlayer_fixture.py`. A relation whose type or related id cannot be read is covered in `tests/unit/test_enum_converters.py`, alongside the converter-side half of the same failure class. Equality and containment are covered in `tests/unit/test_file_identity.py`, and the rule that a field the API did not supply resolves to an absence rather than an error — for the optional parts of a FILE reply — in `tests/unit/test_file_response_decoding.py`.
