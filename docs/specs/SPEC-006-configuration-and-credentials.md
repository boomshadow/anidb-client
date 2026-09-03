---
title: "Configuration and Credentials"
description: "Behavioral expectations for initialising anidb-client: init() as the single required entry point, the database URL and the in-memory URL it refuses outside db_only mode, the connection-pool size it exposes, the three credential sources it resolves (direct arguments, a netrc file, or neither in db_only mode), the exact netrc machine-name matching rules for AniDB credentials, database credentials and the fanart key, safe injection of a netrc-sourced password into the SQL URL, the registered client identity, the encryption key, the pinned outgoing UDP port, the replaceable rate limiter whose resumable state is stated as durations rather than instants so it can outlive the process, logging setup — including a logger that exists from import, so no path reachable before init() can fail by reporting something too early — the refusal of a second init() while one is already live, the all-or-nothing construction that leaves nothing running when init() fails, and close() as the clean shutdown that gives back everything init() took, including the pinned source port."
status: accepted
tags: [configuration, init, credentials, netrc, database-url, sql-url, db-only, in-memory, connection-pool, client-registration, client-name, client-version, encryption-key, api-key, fanart-key, logging, logger, pre-init, udp-port, source-port, port-pinning, rate-limiter, pacing, ban-state, durability, injection, http-timeout, close, shutdown, lifecycle, re-entry, idempotence, teardown, resource-management, engine-disposal]
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

**Rate limiter** — the pacing policy and the ban back-off live in one replaceable object, and `init()` accepts one. Supplying nothing keeps the behaviour every caller has: the transport builds its own.

Why it is exposed at all is a question about *lifetime* rather than about policy. Everything the limiter knows — the burst allowance, the last send, the back-off deadline and how far it has doubled — is per-process, and a restart begins with a clean slate. One burst per deploy is inside AniDB's documented allowance; a crash-looping process repeating it is the shape of the incident this library has already been bitten by. An application that wants that state to survive keeps it wherever it likes and hands back a limiter that already knows it. This library does not choose that storage, which is exactly why the seam is an argument rather than a persistence feature.

**What may be resumed is stated as durations, never as instants.** The back-off deadline is held on a monotonic clock, whose zero point is undefined and process-local, so a deadline stored by one process means nothing in the next — and means nothing *silently*, reading as already elapsed or as hours away with no error either way. So a limiter is told how many seconds of back-off remain and how long ago the last command went out, and converts those to its own clock. Every field that can be seeded has a public reader, so state that can be resumed can also be captured; a limiter that could rehydrate something it had no way to store again would be a worse trap than one that could not resume at all.

**Incoherent state is refused rather than repaired.** A back-off with no multiplier is the case that matters: whether a client is banned is read from the multiplier, so such a limiter would hold a deadline while reporting itself unbanned, and the transport would send straight through it. A ban that exists always has a multiplier of at least one, so a zero says the stored state is already wrong — and silently correcting it would hide that from whoever stored it.

SPEC-002 owns what the limiter does once the transport has it.

**Outgoing UDP port** — one fixed port when not supplied, the same one in every process and on every call. AniDB counts requests against a source address, and a UDP source address includes the port, so which port this client sends from is part of the identity it is metered and banned by (SPEC-002). The default sits above 1024 so binding needs no privilege, and below the usual ephemeral range so the host will not hand it to something else.

An application running several clients at once gives each its own port, because only that application knows how many it is running. Two clients that end up on the same port do not share it: the second fails to bind, at construction, with an error naming the port. ADR-007 records why the default is fixed rather than chosen per call, and why the socket is not made shareable.

**Logging** — a caller may supply its own logger, which is used as-is. Absent one, the library configures a logger at the requested level, attaches a syslog handler, and in debug mode additionally logs to standard error. Credentials are never logged: the AUTH command's contents are suppressed even at debug level, where every other command is logged in full.

The library holds a logger from the moment it is imported, and `init()` replaces or configures that one rather than being what brings it into existence. Logging is therefore never a thing the library has to check for before doing: no code path can fail because it reported something too early. This matters because not every entry point is behind `init()` — the two bulk XML refreshes (SPEC-005) are exported and do only local-file and HTTPS work, so a caller may legitimately reach them first. Before a handler is configured, output follows the standard library's own rule for an unconfigured logger: a warning reaches standard error and anything below it is dropped.

**HTTP timeout** — every HTTP request the library makes (the two bulk XML fetches, cover images and the fanart API) is bounded by a per-socket-operation timeout. This is not a bound on the whole transfer: it ends a stalled connection but not a pathologically slow one. Without it, urllib's default of no timeout at all lets any of those calls block their caller forever on a server that accepts a connection and then stops talking — the one hang the UDP transport's own timeouts do not cover.

## One client at a time

**`init()` refuses to run while a client is already live**, and says so rather than letting the caller find out on the socket. This library holds its client in module state — there is no client object to pass around — and the transport binds one fixed source port that is deliberately not shareable, so a second client could not have opened anyway. What a caller used to get was an address-in-use error naming a port nothing visible was using, raised from a call that reads like configuration. ADR-008 records the decision, and why a silent no-op and a tear-down-and-rebuild are both worse.

`close()` is the way back: after it, `init()` succeeds again.

## Failing to start leaves nothing running

**A failed `init()` acquires nothing, or gives back whatever it had already acquired.** Everything the call can refuse over — the in-memory URL, missing credentials, a database URL that will not parse — is decided before anything is opened. The two things that *are* opened are opened together, cache first, and if the second fails the first is released.

The order is not arbitrary. The cache is the cheaper thing to fail and the easier thing to give back, so it goes first; the transport goes last, so the resource that is hardest to recover is never the one left stranded. This used to run the other way round, and the consequence was sharp rather than untidy: the UDP socket was bound and both of its threads were running fifty lines before the database was touched, so a bad database URL raised with a live socket owned by nothing the caller could reach — and because the port is pinned and not shareable, the caller who corrected the URL and called `init()` again could not bind. Two individually correct decisions that interacted badly.

The one thing a failed `init()` does leave changed is the logger, which is not a resource and which this spec already says exists from import and is configured rather than created.

## Shutting down

`close()` ends the UDP session cleanly, logging out so AniDB is not left holding a session. A caller that skips it leaves the session to expire on the server's schedule. In `db_only` mode there is no transport to stop.

**After `close()` returns, the process is in the state it was in before `init()` ran.** That is the property, and it is worth more than the courtesy of the logout:

- **The transport is stopped and not handed out.** `get_link()` refuses rather than answering with a stopped transport. The health surface it carries (SPEC-002) exists to be believed without sending anything to check it, so an object describing a session that no longer exists is worse than a refusal.
- **The cache's connections are given back.** The engine is disposed rather than left holding its pool. Invisible in a process that initialises once and exits; a genuine leak in anything that initialises and closes more than once.
- **The source port is free.** Not eventually free — free before `close()` returns, so a caller restarting can bind it immediately. SPEC-002 covers what that costs the transport to guarantee, because closing the socket is not by itself enough to achieve it.
- **The fanart key is cleared**, because it is configuration this call is undoing.

**Logging out is best effort; shutting down is not.** A client AniDB has stopped answering can never be told its logout arrived, so the wait for that acknowledgement is bounded and the teardown happens either way. The bound may be shortened by the caller: an application whose own shutdown budget is tighter than the transport's command timeout should say so rather than discover the difference during a deployment.

`close()` on a library that was never initialised, and `close()` twice, both do nothing and neither is an error.

## Related Artifacts

- **Line of truth (external):** the netrc file format, and AniDB's client-registration requirement for the name-and-version pair sent in AUTH.
- **Related ADRs:** ADR-007 (why the outgoing source port is pinned rather than chosen per call); ADR-008 (why a second `init()` is refused rather than ignored or rebuilt).
- **Related specs:** SPEC-002 (what the resolved credentials, client identity, encryption key and outgoing port are used for); SPEC-003 (the database URL's role and the backends it may name); SPEC-005 (the fanart key's effect on `Anime.fanart`); SPEC-001 (objects, none of which may be constructed before `init()` has run).
- **Tests:** credential resolution and the `db_only` path in `tests/unit/test_init_credentials.py`; the in-memory refusal — including that no UDP link is opened before it — and the pool-size argument reaching the engine in `tests/unit/test_init_database.py`; the SQL URL rewriting rules — hostname matching, user pairing, percent-encoding, IPv6 and ports — in `tests/unit/test_sql_url_credentials.py`; the outgoing UDP port — that the default is the pinned one, that it does not move between calls, and that a caller may still choose its own — in `tests/unit/test_init_udp_port.py`; the rate limiter this call accepts in `TestTheLimiterInitIsGiven` in `tests/unit/test_ratelimit.py`, which pins that the limiter given is the one handed to the transport, that omitting it leaves the transport to build its own, that a resumed one arrives still banned, and that both it and the ban vocabulary are named in the declared public surface; the HTTP timeout's presence at every call site in `tests/unit/test_http_timeouts.py`; that a logger exists before `init()` and that the entry points reachable that early report rather than raising, in `tests/unit/test_xml_cache_fetch.py`; the package's declared public surface in `tests/unit/test_package.py`. The lifecycle is covered in `tests/unit/test_lifecycle.py`: that a second `init()` is refused, that the refusal names the way out and leaves the first client working, and that closing first makes a second call legal; that a failed `init()` builds no transport when the cache is what refused, disposes the cache when the transport is what refused, leaves no global set and no fanart key behind, and lets a corrected call succeed; that `close()` disposes the engine, drops the session factory, clears the key, stops the transport, refuses to hand a stopped one out, and still returns the pool when stopping raises; and that closing twice or without having initialised is a no-op. The reported failure is pinned there end to end with a real socket, in `TestThePinnedPortComesBack`: after `close()` the same port binds again, immediately and in the same process.
