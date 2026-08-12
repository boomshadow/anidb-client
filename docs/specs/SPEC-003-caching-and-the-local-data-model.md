---
title: "Caching and the Local Data Model"
description: "Behavioral expectations for anidb-client's local cache: the SQL database (SQLite, PostgreSQL or MySQL) that fronts every AniDB lookup, the freshness policy — a hard one-day floor, a once-per-20-hours freshness roll, and an age-weighted refresh probability that warms a cache gradually rather than expiring it all at once — the anime-specific bonus derived from how recently AniDB itself changed the record, the on-disk XML caches refreshed at most every 36 hours, what the cache schema does and does not guarantee, and the absence of any migration story."
status: accepted
tags: [cache, caching, freshness, refresh-probability, dice, sqlalchemy, sqlite, postgresql, mysql, schema, data-model, enums, xml-cache, anime-titles, anime-list, migrations, upgrade, ban-avoidance]
---

# Caching and the Local Data Model

The cache is not an optimisation here; it is the primary defence. AniDB bans clients that talk to it too often, so the library is built to answer from local storage and go to the network only when it must. This spec describes when cached data is considered good enough and what the local store guarantees.

## The two caches

Two distinct caches sit in front of AniDB, with different lifetimes and different failure behavior:

- **The SQL cache** holds everything fetched over the UDP API — anime, episodes, files, groups and their relations. Its location is the database URL passed to `init()` (SPEC-006), and it may be SQLite, PostgreSQL or MySQL.
- **The on-disk XML caches** hold the two bulk files fetched over HTTPS: AniDB's anime-titles export and the Anime-Lists external-id mappings (SPEC-005). They live in the system temporary directory.

## Freshness in the SQL cache

An object with no cached row at all is fetched immediately and blocks the caller. Everything else goes through the freshness policy, which is deliberately reluctant to spend a network call:

**A hard floor of one day.** Data fetched less than a day ago is never re-fetched, whatever else is true. This is the floor the whole policy rests on.

**A freshness roll at most once every 20 hours.** Past the floor, the decision to refresh is a weighted coin flip, and the result of *having flipped* is recorded. Another read within 20 hours does not flip again. Twenty hours rather than twenty-four so that a daily cron job still gets a fresh decision every run instead of drifting into skipping days.

**Odds that grow with age.** The refresh probability is nothing for the first week, a couple of percent in the second, and grows by roughly half again for each further week, capped at certainty. The intent is that a large collection warms up over time instead of the whole cache expiring at once and firing a stampede of rate-limited calls.

**An anime-specific bonus.** For anime the odds carry an extra term derived from how close together AniDB's own last change to the record and this client's fetch of it were. A record fetched shortly after AniDB changed it is more likely to change again soon, so it starts with a meaningful bonus that decays week by week to nothing.

The policy is heuristic. It is not claimed to be optimal for every collection, and `update()` (SPEC-001) is the documented way for a caller who knows better to override it.

## Freshness in the XML caches

Each bulk XML file is re-fetched only if the copy on disk is older than 36 hours. Deleting the cached file forces an immediate refresh on next use.

A download is written to a temporary file and only moved into place once it parses and looks complete — a truncated download still parses as XML, so size is the sanity check. A fetch that fails leaves any existing cached copy in place and in use: being unable to refresh is not a reason to lose what is already held, and a failed fetch here is a routine outcome when AniDB has temporarily IP-banned the caller. Only when there is no usable copy at all does the failure reach the caller as an error.

## What the cache stores

The schema is defined by the SQLAlchemy declarative models and is created automatically when the database is opened, so the models are their own definition and this spec does not restate their columns. What the schema alone does not convey:

- **Anime, episode, file and group rows each carry two timestamps**: when the row was last fetched, and when the freshness roll was last made for it. The policy above is expressed entirely in those two.
- **Relations are rows, not lists.** Anime-to-anime and group-to-group relations are separate tables with a constrained relation type, refreshed by reconciling against what AniDB last reported — matching links are updated in place, new ones added, and links AniDB no longer reports removed.
- **Constrained vocabularies are enforced by the schema, and defined only there.** Relation types, episode types, and the mylist state and file-state vocabularies are database-level enumerations. On PostgreSQL they become native enum types; on SQLite they are plain text. A value outside the vocabulary is a schema violation, not a silently stored string. The wire layer that converts AniDB's numeric response codes *selects from* these vocabularies rather than restating them, so the two cannot disagree; they are stored as AniDB words them, not as the enum members are named.
- **Large identifiers vary by backend.** The schema declares wide integer columns with a narrower variant on SQLite, because the two backends differ in what they will accept. Any test that means to cover both branches has to run against both.
- **A file row can exist without AniDB knowing the file.** Generic entries (SPEC-004) are cached rows describing an anime and episode with no file identity, and are marked as such.

Writes are best-effort in the sense that a database error during a cache update is logged and rolled back rather than propagated: failing to *cache* a value must not fail the caller's read of it.

### Cache identity for files

A file cached by path is re-validated on load: if the file at that path no longer has the size that was cached, the cached row is discarded rather than trusted, and identification starts again. Failing that, a file is looked up by its size and ed2k hash together, which is how AniDB itself identifies files (SPEC-004).

## Upgrading

**The cache has no migration story.** There is no schema versioning, no migration tool and no upgrade path. When the schema changes between releases, the documented procedure is to delete the database and let it repopulate from AniDB as it is used.

This is a deliberate consequence of what the store is: a cache. It holds nothing that cannot be re-fetched, so the cost of discarding it is time and API calls rather than data loss — and carrying a migration framework for a rebuildable cache would be more machinery than the problem warrants. The obligation this creates is on the release notes: a schema change is a breaking change for anyone who upgrades in place.

## Related Artifacts

- **Line of truth (self-enforcing):** `src/anidb_client/db.py` — the SQLAlchemy declarative models, executed to create the cache tables. When this spec and a model disagree about the schema, the model is correct.
- **Related specs:** SPEC-001 (attribute resolution, which consults this cache before the network); SPEC-002 (the transport this cache exists to keep idle); SPEC-004 (how a file is identified before it can be cached); SPEC-005 (the XML caches' contents and how they are matched against); SPEC-006 (the database URL and its credentials).
- **Tests:** freshness policy in `tests/unit/test_cache_freshness.py`, structured around the floor, the roll cooldown, the age curve and the anime bonus. Schema behavior — round trips, enum constraints, relationships and the update helper — lives in `tests/unit/test_db.py`, which also pins that the vocabularies are stored as AniDB words them rather than as their members are named, since every other assertion would pass either way. The PostgreSQL-only half is in `tests/integration/test_schema_postgres.py` behind the `postgres` marker, because SQLite exercises neither the native enum types nor the wide-integer variant; the native type's labels are checked there for the same reason. That the wire layer selects from these vocabularies rather than restating them is enforced from the other side, in `tests/unit/test_enum_converters.py`, which checks that every conversion table holds members and that no vocabulary entry is left unreachable.
