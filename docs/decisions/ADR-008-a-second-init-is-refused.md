---
title: "A Second init() Is Refused"
description: "anidb-client raises when init() is called while a client is already live, rather than quietly ignoring the call or tearing the existing client down and rebuilding it. The library holds one client in module globals and its transport binds one fixed, unshareable UDP source port, so a second init() could not have opened a working client anyway — it failed on the socket, with an address-in-use error naming a port nothing visible was using, from a call that reads like configuration. A silent no-op was rejected because it leaves the caller believing it reconfigured something it did not, with the disagreement surfacing later and somewhere else; tear-down-and-rebuild was rejected because it discards a live authenticated session and makes the next command pay a fresh handshake against a rate-limited API, for a call that is most likely a mistake; and supporting several concurrent clients was rejected as a much larger change that the module-global design, not this check, is what forbids. close() is the supported way back to an uninitialised library."
status: accepted
tags: [init, close, lifecycle, idempotence, re-entry, module-globals, single-client, udp, source-port, port-pinning, so-reuseaddr, fail-fast, configuration, resource-management, teardown]
---

# A Second init() Is Refused

## Context

`init()` is the single supported entry point (SPEC-006). It resolves credentials, opens the cache and opens the UDP session, and it publishes the results into module globals — there is no client object to hold, so the library *is* the client.

That design admits exactly one live client per process, and until now nothing said so. Calling `init()` twice was not checked, so it went ahead and built a second one. The second one then failed, but not for the reason the caller had done anything about: `AniDBLink` binds one fixed UDP source port and no longer sets `SO_REUSEADDR` (ADR-007), so the second bind hit the port the first client was still holding. What reached the caller was an address-in-use error naming a port nothing visible on the host was using, raised from a call that reads like configuration rather than like opening a socket.

The same absence made the failure worse than untidy. A second `init()` that failed part-way through left the first client's globals partially overwritten, so the process ended up neither cleanly on the old client nor on a new one.

There is a related asymmetry worth stating, because it is why this decision can be made at all. `close()` did not clear what it stopped — it declared the module globals and assigned none of them — so there was no state the library recognised as *uninitialised*. A rule about "already initialised" needs a way back, and until `close()` genuinely undid `init()` there was not one.

## Decision

**`init()` raises when a client is already live in this process.** The error says that `init()` has already been called, why one client is the limit, and that `close()` is the way to start again.

`close()` is the counterpart: it stops the transport, disposes the cache engine, clears the globals and returns the library to the state it was in before `init()` ran. After it, `init()` succeeds again. The pair is what makes the refusal a rule rather than a dead end.

The principle is ADR-007's, one level up: **this client would rather fail to start than be quietly wrong about its own identity.** There it was about which source port the socket presents; here it is about which client the module globals describe.

Three alternatives were considered and rejected.

**Make a second `init()` a silent no-op.** The most common shape for an idempotent initialiser, and the worst of the three here. A caller that passes a different database URL, a different port or different credentials has expressed an intent, and answering it by doing nothing lets that caller believe it reconfigured something it did not. The disagreement then surfaces at some later, unrelated call — a query against the wrong cache, or traffic from the wrong identity — where nothing points back at the `init()` that was ignored. A no-op is only honest when the second call *asks for the same thing*, and this one has no way to know that: comparing the arguments would mean deciding when two database URLs, two netrc paths and two loggers are equivalent, which is a judgement this library has no basis to make.

**Tear the existing client down and rebuild it.** Attractive because it makes the call mean what it says. Rejected because of what it costs against a rate-limited service. It discards a live authenticated session, so the next command pays a fresh handshake — and handshakes are the traffic AniDB counts most closely, the thing the pacing and back-off rules exist to ration. Spending one on a call that is most likely a duplicate is a bad trade, and it is a trade made silently. It is also the most dangerous of the three when the second call is itself a bug: a retry loop that accidentally re-initialises would tear down a working client on every pass and reauthenticate its way into a ban, which is precisely the incident ADR-007 was written about.

**Support several concurrent clients.** The honest version of what a caller reaching for a second `init()` might actually want. Rejected as out of scope rather than as wrong: it is a different library shape, in which the client is an object a caller holds rather than state a module owns, and every accessor here — `get_session()`, `get_link()`, the object layer's construction — would have to take a client. That is a large change with a real design question underneath it, and it should be argued on its own rather than arrived at by loosening a guard. Nothing here forecloses it. Note also that the constraint is not this check: it is the module globals and the single pinned port. The check only makes the existing limit say so.

## Consequences

**A caller that re-initialises must close first.** This is a behaviour change for anyone whose code called `init()` twice — though "worked" overstates what they had, since the second call already failed on the socket for every client that was not `db_only`. A `db_only` client is the case that genuinely changes: two `init()` calls used to succeed and silently replace the first cache with the second.

**`init()` and `close()` are now a pair, and the tests read as cycles.** A test that wanted to prove something about two clients in a row — that the source port does not move between them, for instance — now spells out `init` / `close` / `init`. That is the truer statement of the question anyway: what matters is that a service restarting presents the same port it did before, not that two clients can coexist in one process.

**The failure is attributable.** A caller that initialises twice by accident is told so, in terms naming its own API call, instead of being handed a socket error about a port it never chose.

**There is now a state the library calls uninitialised, and it is reachable.** That is worth more than the refusal itself: it is what lets a failed `init()` leave nothing behind, lets `close()` be called twice without complaint, and lets an embedding application's shutdown be verified rather than assumed.
