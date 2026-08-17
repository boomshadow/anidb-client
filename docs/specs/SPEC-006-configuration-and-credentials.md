---
title: "Configuration and Credentials"
description: "Behavioral expectations for initialising anidb-client: init() as the single required entry point, the database URL and the in-memory URL it refuses outside db_only mode, the connection-pool size it exposes, the three credential sources it resolves (direct arguments, a netrc file, or neither in db_only mode), the exact netrc machine-name matching rules for AniDB credentials, database credentials and the fanart key, safe injection of a netrc-sourced password into the SQL URL, the registered client identity, the encryption key, logging setup — including a logger that exists from import, so no path reachable before init() can fail by reporting something too early — and close() as the clean shutdown."
status: accepted
tags: [configuration, init, credentials, netrc, database-url, sql-url, db-only, in-memory, connection-pool, client-registration, client-name, client-version, encryption-key, api-key, fanart-key, logging, logger, pre-init, udp-port, http-timeout, close, shutdown]
---

# Configuration and Credentials

`init()` is the one thing a caller must do before anything else works. It resolves credentials, opens the UDP session unless told not to, and opens the cache database. This spec describes what it accepts and, in particular, how it finds credentials the caller did not pass directly.

## The single required argument

The database URL is the only required argument. Everything else is optional, and the optional arguments interact in ways worth stating explicitly.

### The one URL that is refused

**An in-memory SQLite database is refused unless `db_only` is set**, and the error says why rather than letting the caller find out later that nothing works.

Outside `db_only` this library runs a callback thread per API reply, and each thread is handed its own connection. Every connection to an in-memory database is *a separate database*: the tables are created on the thread that called `init()`, and every other thread finds a database with nothing in it. Passing such a URL used to succeed and then silently answer nothing useful.

No setting fixes this. Sharing a single connection across the threads would make the tables visible, but the pool that does so is documented as supporting no form of concurrency at all. It is a mismatch between an in-memory database and this library's threading model, not a missing flag.

In `db_only` mode there are no callback threads and no UDP session, and an in-memory cache works. That is the realistic use of one, and it stays supported.

## Credential resolution

AniDB credentials come from exactly one of three arrangements:

1. **Passed directly** as username and password arguments.
2. **Read from a netrc file** when a netrc path is supplied and the arguments are absent.
3. **Not needed at all** in `db_only` mode.

`db_only` is the cache-only mode: no UDP session is opened, and everything is answered from the local database or not at all. Because it never talks to AniDB, it must not demand AniDB credentials — requiring them would make the mode refuse to start for exactly the offline use it exists to serve.

Outside `db_only`, having neither direct credentials nor a usable netrc file is an error at init time rather than a failure at first use.

## netrc lookup rules

A netrc file is consulted for three separate secrets, each keyed by its own set of machine names. These names are matched exactly, and getting one wrong means the lookup silently finds nothing — so they are stated here rather than left to be discovered.

**AniDB credentials** — machine name must be one of `api.anidb.net`, `api.anidb.info` or `anidb.net`. The `login` and `password` fields carry the account credentials. The `account` field, if present, carries the encryption key, and it is used only when no key was passed directly.

**Database credentials** — machine name must match the **hostname** from the database URL, and only the hostname: no port, and no brackets around an IPv6 literal. Matching is case-insensitive.

**The fanart.tv API key** — machine name must be one of `fanart.tv`, `assets.fanart.tv`, `webservice.fanart.tv` or `api.fanart.tv`. The key may be in either the `account` or the `password` field.

### Injecting a database password

The database lookup applies only when the URL carries no password of its own; a password already in the URL is left alone. When a netrc password is found, two further rules apply:

- **The credential must belong to the user named in the URL.** netrc holds one credential per host, and pairing it with a different username would just fail authentication confusingly. When the URL names no user, the netrc login is used. netrc also permits an entry with a password and no login at all: such a credential belongs to no user, so it is left unused whether or not the URL names one.
- **The URL is rebuilt structurally, not by string surgery.** The username and password are percent-encoded before being placed into the URL, so a password containing URL-significant characters produces a URL that still parses as intended. An IPv6 literal host is re-bracketed and any port is preserved.

## Client identity

The AUTH command carries a client name and an integer client version, and AniDB refuses to authenticate a pair it has not registered. The defaults identify this library. An application embedding the library registers its own pair and passes it to `init()`.

The registered client version is deliberately unrelated to the distribution's own version: upgrading the installed package does not change the identity AniDB sees, and it should not.

## Other settings

**Encryption key** — enabling an encrypted session is the user's choice, not a default. Supplying a key here (or via netrc) turns encryption on; see SPEC-002 for what that changes about session establishment.

**Database connection-pool size** — the cache's pool is bounded (SPEC-003), and its size is an argument here. The default is modest and suits a client of this library; an application that knows its own concurrency raises or lowers it rather than living with a number this library picked for it. The burst allowance above that size is not exposed: the bound is the decision, not its shape.

**Outgoing UDP port** — chosen at random within a fixed range when not supplied. The choice is made per `init()` call rather than once per process, so several clients in one process do not collide on a port fixed at import time.

**Logging** — a caller may supply its own logger, which is used as-is. Absent one, the library configures a logger at the requested level, attaches a syslog handler, and in debug mode additionally logs to standard error. Credentials are never logged: the AUTH command's contents are suppressed even at debug level, where every other command is logged in full.

The library holds a logger from the moment it is imported, and `init()` replaces or configures that one rather than being what brings it into existence. Logging is therefore never a thing the library has to check for before doing: no code path can fail because it reported something too early. This matters because not every entry point is behind `init()` — the two bulk XML refreshes (SPEC-005) are exported and do only local-file and HTTPS work, so a caller may legitimately reach them first. Before a handler is configured, output follows the standard library's own rule for an unconfigured logger: a warning reaches standard error and anything below it is dropped.

**HTTP timeout** — every HTTP request the library makes (the two bulk XML fetches, cover images and the fanart API) is bounded by a per-socket-operation timeout. This is not a bound on the whole transfer: it ends a stalled connection but not a pathologically slow one. Without it, urllib's default of no timeout at all lets any of those calls block their caller forever on a server that accepts a connection and then stops talking — the one hang the UDP transport's own timeouts do not cover.

## Shutting down

`close()` ends the UDP session cleanly, logging out so AniDB is not left holding a session. A caller that skips it leaves the session to expire on the server's schedule. In `db_only` mode there is nothing to close.

## Related Artifacts

- **Line of truth (external):** the netrc file format, and AniDB's client-registration requirement for the name-and-version pair sent in AUTH.
- **Related specs:** SPEC-002 (what the resolved credentials, client identity and encryption key are used for); SPEC-003 (the database URL's role and the backends it may name); SPEC-005 (the fanart key's effect on `Anime.fanart`); SPEC-001 (objects, none of which may be constructed before `init()` has run).
- **Tests:** credential resolution and the `db_only` path in `tests/unit/test_init_credentials.py`; the in-memory refusal — including that no UDP link is opened before it — and the pool-size argument reaching the engine in `tests/unit/test_init_database.py`; the SQL URL rewriting rules — hostname matching, user pairing, percent-encoding, IPv6 and ports — in `tests/unit/test_sql_url_credentials.py`; the HTTP timeout's presence at every call site in `tests/unit/test_http_timeouts.py`; that a logger exists before `init()` and that the entry points reachable that early report rather than raising, in `tests/unit/test_xml_cache_fetch.py`; the package's declared public surface in `tests/unit/test_package.py`.
