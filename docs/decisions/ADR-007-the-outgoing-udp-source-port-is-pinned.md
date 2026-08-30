---
title: "The Outgoing UDP Source Port Is Pinned"
description: "anidb-client binds one fixed outgoing UDP source port by default and refuses to share it, because AniDB counts requests against a source address and reads a spray of source ports from one IP as the flooding it bans for — an IP ban this library caused by rolling a fresh random port on every init(). A random port per init() was rejected because it turns a correct per-process client into a fleet of apparent clients that no single process can see itself joining; making the port a required argument was rejected because a value every caller must supply and none can reason about is a default written in the caller's file; install-time configuration alone was rejected because it leaves the out-of-the-box behaviour undefined at exactly the moment a first-time user is most likely to be banned; and SO_REUSEADDR was rejected because it makes a duplicate bind succeed silently and split one client's replies between two processes."
status: accepted
tags: [udp, source-port, port-pinning, so-reuseaddr, socket, ban, ip-ban, rate-limiting, flood-protection, nat, defaults, configuration, breaking-change, incident]
---

# The Outgoing UDP Source Port Is Pinned

## Context

AniDB's UDP API is rate limited per client, and its enforcement is an IP ban rather than an error reply. What the service counts requests against is the **source address a datagram arrives from** — and a UDP source address is a host *and a port*.

This library used to choose that port at random, in a fixed range, on every `init()` call. The comment recording the reasoning said the choice was made per call rather than once per process so that several clients in one process would not collide on a port fixed at import time. That is a real problem, and this was the wrong solution to it: it treated the port as scratch space to be kept out of its own way, when the port is part of the identity AniDB is metering.

**The incident.** An embedding application constructed a client per operation, across a series of short-lived processes. Each process was well behaved on its own — it paced its commands, it authenticated once, it logged out. Between them they presented dozens of distinct UDP source ports from one IP address inside an hour, which is what a flood looks like from the service's side, and the IP was banned. Nothing in any individual process's behaviour was visibly wrong, and nothing in any individual process could see the shape of the whole.

AniDB's own published guidance points the other way from what the library did: select a fixed local port above 1024 at install time and reuse it.

There is a second, quieter way to lose the same identity. The socket was bound with `SO_REUSEADDR`. On Linux that permits a second process to bind a port this one already holds, with no error raised on either side; the kernel then delivers each incoming datagram to one of the two. The starved client sees replies stop arriving, times out, retries — and earns a ban for a collision it has no way to observe. This has been reproduced.

## Decision

**The outgoing UDP source port is one fixed port, and the client will not share it.**

- The default is a single port — `9876`, which is already `AniDBLink`'s own default — used by every `init()` in every process unless the caller says otherwise. It is above 1024 so binding needs no privilege, and below the usual Linux ephemeral floor of 32768 so the kernel will not hand it to something else on the same host.
- `outgoing_udp_port` remains an argument. An application running several clients at once gives each its own port, because only that application knows how many it is running.
- **`SO_REUSEADDR` is not set.** A duplicate bind fails, loudly, at construction, with an error naming the port and saying why this client insists on having one to itself.

The principle underneath both halves: **the socket has one stable identity, and the client would rather fail to start than be quietly wrong about what that identity is.** A ban is expensive and slow to discover; a bind error is cheap and immediate.

This inverts the previous default, and it is a behaviour change for anyone relying on the old one. The version number that carries it is chosen by whoever cuts the release, not here.

Three alternatives were considered and rejected.

**Keep the random port per `init()`.** Rejected on the incident. Its stated benefit — several clients in one process not colliding — is real but narrow, and it is bought by making every client in every process a different client in AniDB's eyes. The failure it causes is worse than the one it prevents in three ways: a port collision fails immediately and locally, where a ban arrives hours later and lands on the whole host; the collision is visible to the process it happens in, where the ban is invisible to every process that contributed to it; and the collision affects one application, where the ban affects everything on the IP, including whoever runs the test suite next. The narrow benefit is also still available, as an argument, to the callers who actually need it — and those callers are exactly the ones in a position to know.

**Make `outgoing_udp_port` required rather than defaulted.** Superficially attractive: it forces the caller to make the decision, and the caller is who AniDB holds responsible. Rejected because it is a default written in someone else's file. Nothing a first-time caller knows helps them choose a number, so the value they pass will be whatever the README showed them — which is a default with extra steps, and one that cannot be corrected by upgrading. It also breaks every existing caller to no behavioural end, since the number they would supply is the one this library would have picked.

**Leave the port to install-time configuration only, with no library default.** This is closest to AniDB's own wording, and it is right about where the decision belongs for a deployed application. Rejected as the *library's* answer because it says nothing about what happens when nobody configures anything — and the moment nobody has configured anything is a first run, which is exactly when a user is most likely to be banned and least likely to understand why. A library that ships a defensible default and documents how to override it gets the same outcome for the configured case and a much better one for the unconfigured case. The configuration story is unaffected: the argument is still there for an installer to fill in.

**Keep `SO_REUSEADDR` so a restart can rebind promptly.** Rejected because the promptness it buys does not exist for this socket. `SO_REUSEADDR` addresses TCP's `TIME_WAIT`, which UDP has none of; what it does here is permit exactly the duplicate bind this decision exists to prevent. Trading a loud failure at startup for a silent one that manifests as an unexplained ban is the wrong direction for every value of promptness.

## Consequences

**Two clients on one host must be told apart by their caller.** An application running several links at once passes each a distinct `outgoing_udp_port`; one that does not will now find out at construction rather than by way of two clients sharing a socket. This is the case the random port existed to serve, and the cost of moving it to the caller is one argument in the caller that already knows the answer.

**A port clash is a startup error rather than a mystery.** The failure names the port and the reason, so the operator's next step is obvious. Previously the same situation either succeeded and misbehaved (with `SO_REUSEADDR`) or produced a bare `EADDRINUSE` from inside a constructor.

**AniDB sees one client per host, consistently, however the application is structured.** Short-lived processes, a long-running daemon and a script run by hand all present the same source address, which is the identity AniDB's rate limiting is designed around.

**The pinned port is now a thing a deployment has to know about.** It is a fixed number on the host, so it can collide with something unrelated, and it may need opening in a firewall. That is the ordinary cost of a stable identity and the reason the argument stays.
