---
title: "Caching and the Local Data Model"
description: "Behavioral expectations for anidb-client's local cache: the SQL database (SQLite, PostgreSQL or MySQL) that fronts every AniDB lookup, the freshness policy — a hard one-day floor, a once-per-20-hours freshness roll, and an age-weighted refresh probability that warms a cache gradually rather than expiring it all at once — the anime-specific bonus derived from how recently AniDB itself changed the record, the on-disk XML caches refreshed at most every 36 hours, what the cache schema does and does not guarantee — including that nullability is stated on every column rather than inferred from its annotation — how the engine is configured for a threaded process (write-ahead logging attempted with a fallback, an explicit busy timeout, enforced foreign keys and a bounded connection pool), the block-scoped lifetime of a cache session, the absence of any migration story, and the DDL snapshot that makes an unintended schema change a build failure rather than a silent cache rebuild for every user."
status: accepted
tags: [cache, caching, freshness, refresh-probability, dice, sqlalchemy, sqlite, postgresql, mysql, schema, data-model, ddl, schema-snapshot, nullability, mapped-column, type-annotations, enums, xml-cache, anime-titles, anime-list, migrations, upgrade, ban-avoidance, wal, journal-mode, busy-timeout, foreign-keys, connection-pool, concurrency, sessions, connection-leak, best-effort-writes]
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
- **Nullability is stated, never inferred.** The models are declared in typed style, where SQLAlchemy will take a column's nullability from whether its annotation admits `None`. It is deliberately not allowed to: every column says `nullable=` outright, so what reaches the database is decided by one thing rather than by two that can disagree. Editing an annotation must not be able to change the schema, because a schema change here costs every user a cache rebuild.
- **A file row can exist without AniDB knowing the file.** Generic entries (SPEC-004) are cached rows describing an anime and episode with no file identity, and are marked as such.

Writes are best-effort in the sense that a database error during a cache update is logged and rolled back rather than propagated: failing to *cache* a value must not fail the caller's read of it.

**A cache session lives for a block, not for a pair of statements.** That best-effort rule is exactly what makes the lifetime worth stating: an error between opening a session and closing it is logged and goes no further, so a close that never ran left a pooled connection held with nothing raised to say so — and the paths where that happened are the error branches and the early returns, which are the least exercised. Every use of a session is therefore scoped to a block, and the connection goes back whether the block ends normally, returns from the middle, or raises.

The block only pairs the open with the close. It does not commit and it does not swallow: what is committed, and which errors are logged rather than raised, is decided by the code inside it, as before. SQLAlchemy's own transactional block would commit on success and re-raise on failure, which is the opposite of the rule above.

## How the database is opened

How the engine is created is a decision about the environment this library actually runs in: a threaded process, most often on SQLite — every `init()` example a user is likely to copy names a SQLite file, and PostgreSQL is the *tested* backend rather than the *documented* one. SQLite's defaults assume neither the threading nor the embedding.

**Write-ahead logging, asked for rather than required.** A SQLite cache is put into WAL mode, because the default journal mode lets one writer exclude every reader for the length of the busy timeout, and this library runs a callback thread per API reply — so that contention is not hypothetical. WAL is attempted, not demanded: it does not work over a network filesystem, and this package supports `nfs://` paths, so a cache on a NAS is a configuration somebody has. It also creates companion files next to a database file the user owns. Setting the mode reports the mode that resulted, so the mode obtained is read back and logged rather than assumed, and a refusal is carried on from rather than raised.

**An explicitly chosen busy timeout.** How long a write waits behind another before failing is decided where the engine is created rather than inherited: WAL makes write contention rarer without removing it, and an inherited default is one nobody decided. It is set through the driver's own connect argument, which is exactly SQLite's busy timeout — a pragma accepts no bound parameter, so setting it in SQL would mean formatting a value into a statement, which is the shape of an injection bug whether or not the value is a constant.

**Foreign keys are enforced.** SQLite ignores foreign-key constraints unless each connection turns them on, so the constraints this schema declares used to do nothing — the cascade held because the ORM performed it in Python, not because the database refused anything. Every SQLite connection enables enforcement as it is opened, which makes the guarantee a reader would assume from the schema a real one. Enforcement does not change what deleting a row does; it changes what an inconsistent write does.

**The connection pool is bounded, and the bound is the caller's to move.** The pool keeps a fixed number of connections and may open a small further burst before refusing. It is not unlimited, as it once was: this library runs inside somebody else's application, and an unbounded pool means a connection leak here is paid for by whatever reaches its own limit first — a PostgreSQL server's connection slots, or the process's file descriptors — usually far enough from the cause to be attributed to the wrong thing. A bound turns that into a fast, loud, attributable error. An application that knows its own concurrency overrides the size through `init()` (SPEC-006). Note that a larger pool buys SQLite nothing regardless, since only one connection can write at a time.

**These settings are SQLite's alone.** The pragmas are emitted only for a SQLite dialect; a server database gets the pool bound and nothing else.

**An in-memory cache is refused outside cache-only mode.** The rule and its reasoning are in SPEC-006, because it is `init()` that refuses.

### Cache identity for files

A file cached by path is re-validated on load: if the file at that path no longer has the size that was cached, the cached row is discarded rather than trusted, and identification starts again. Failing that, a file is looked up by its size and ed2k hash together, which is how AniDB itself identifies files (SPEC-004).

## Upgrading

**The cache has no migration story.** There is no schema versioning, no migration tool and no upgrade path. When the schema changes between releases, the documented procedure is to delete the database and let it repopulate from AniDB as it is used.

This is a deliberate consequence of what the store is: a cache. It holds nothing that cannot be re-fetched, so the cost of discarding it is time and API calls rather than data loss — and carrying a migration framework for a rebuildable cache would be more machinery than the problem warrants. The obligation this creates is on the release notes: a schema change is a breaking change for anyone who upgrades in place.

It is also what makes enforcing foreign keys safe to turn on. Enforcement can turn rows that were already inconsistent into errors, which in a store that carried its contents across versions would be a migration problem. Nobody carries rows forward here, so there are none to break.

**Because the upgrade is a rebuild, the schema is snapshotted rather than merely described.** Having no migration story makes an accidental schema change survivable; it does not make it acceptable, since every user pays for one with a discarded cache and a fresh run at AniDB's rate limit. So the models are compiled to the DDL each backend would be sent, and that rendering is held against a stored copy for both SQLite and PostgreSQL — two, because the wide-integer variant and the native enum types exist precisely so the two backends differ, and a snapshot of one would not notice a change confined to the other. Anything the compiler emits differently fails the build: a column, a type, a nullability, an index, a constraint, or an enum label or its position. Changing the schema therefore means regenerating the snapshot in the same commit, which is the point — it makes a schema change a visible, argued change rather than something that rode along inside a refactor.

## Related Artifacts

- **Line of truth (self-enforcing):** `src/anidb_client/db.py` — the SQLAlchemy declarative models, executed to create the cache tables. When this spec and a model disagree about the schema, the model is correct.
- **Related specs:** SPEC-001 (attribute resolution, which consults this cache before the network); SPEC-002 (the transport this cache exists to keep idle); SPEC-004 (how a file is identified before it can be cached); SPEC-005 (the XML caches' contents and how they are matched against); SPEC-006 (the database URL and its credentials).
- **Snapshot:** `tests/schema_snapshots/sqlite.sql` and `tests/schema_snapshots/postgresql.sql` — the rendered DDL, regenerated by `task schema-snapshot`. Not a line of truth: `db.py` is, and these are what proves it did not move unnoticed.
- **Tests:** freshness policy in `tests/unit/test_cache_freshness.py`, structured around the floor, the roll cooldown, the age curve and the anime bonus. Schema behavior — round trips, enum constraints, relationships and the update helper — lives in `tests/unit/test_db.py`, which also pins that the vocabularies are stored as AniDB words them rather than as their members are named, since every other assertion would pass either way. The engine configuration is pinned in the same file: the journal mode actually obtained on a file database, that a refused mode still yields a working cache, the busy timeout, foreign keys refusing a genuine violation, and the pool bound and its override. Those tests open the cache through `init_db()`, and so does the shared fixture the rest of the file uses, so the schema is exercised against the configuration real callers get rather than against SQLite's defaults. Session lifetime is covered in `tests/unit/test_cache_session_lifecycle.py`, which asserts on the pool's checked-out count rather than on the mechanism: a block that raises and a block that returns early both give their connection back, and so do the two swallowed-error branches — a failed mylist lookup that answers None, and a failed cache write that still releases the waiter — each provoked with a failure that happens *after* the connection is in hand, since one raised before that would prove nothing. The PostgreSQL-only half is in `tests/integration/test_schema_postgres.py` behind the `postgres` marker, because SQLite exercises neither the native enum types nor the wide-integer variant; the native type's labels are checked there for the same reason. The snapshot is split the same way and shares one renderer in `tests/schema_snapshot.py`: the SQLite half is a unit test and runs everywhere, while the PostgreSQL half sits in the marked file, compiled through a connected engine's dialect rather than a bare one — so it is the DDL a real server was sent — alongside a read of `pg_enum` that holds every vocabulary and its stored order against what the models declare, which is a rendering only a server can confirm was accepted. That the wire layer selects from these vocabularies rather than restating them is enforced from the other side, in `tests/unit/test_enum_converters.py`, which checks that every conversion table holds members and that no vocabulary entry is left unreachable.
