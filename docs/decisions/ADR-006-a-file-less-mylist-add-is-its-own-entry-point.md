---
title: "A File-less Mylist Add Is Its Own Entry Point"
description: "Recording an episode in a mylist without a file on disk is a separate method on Episode rather than a mode of File.update_mylist(), because that method enforces one entry per episode by removing whatever already covers the episode — which would destroy a file-backed entry added from another client — and because it spends four metered requests where the protocol needs one. The new path sends no edit parameter, so AniDB itself refuses to modify an existing entry, and it writes nothing to the local cache: a generic add is answered with a count and no identifier, and this library's cached mylist rows are built around one. Routing through File behind a flag was rejected as keeping the destructive probe; a per-season batch was rejected because a transport failure mid-season discards the record of what already landed; writing a cache row without an identifier was rejected as invisible to every reader; and spending a second request to recover the entry and cache it faithfully was deferred rather than rejected."
status: accepted
tags: [mylist, mylistadd, generic-file, additive, idempotent, rate-limiting, ban-avoidance, api-design, cache, division-of-labour, kiss, deferred-work, error-contract, episode-number, protocol-overloading]
---

# A File-less Mylist Add Is Its Own Entry Point

## Context

This library's mylist surface is reachable only through `File`. A caller supplies a path, the file is hashed, and `update_mylist()` proceeds from the identity AniDB recognises — size plus ed2k hash. That is the right shape for the use case the library was written for: files on disk, added to a list that records what you have.

It is the wrong shape for a media pipeline that re-encodes what it keeps. A remuxed file's hash matches nothing AniDB holds and never will, so file identity is not a weak signal there — it is a dead end. What such a caller does hold, with certainty, is an anime id and the episodes it handled, in AniDB's own vocabulary. That is exactly the input AniDB's own web form takes when it creates a generic, file-less mylist entry, and `MYLISTADD` accepts it over UDP as `aid` + `generic=1` + `epno`.

Two properties of `update_mylist()` make it unusable for that even though it can construct the same command.

**It removes before it adds.** The one-entry-per-episode rule is enforced by deleting whatever entry already covers the episode — consulting the local cache first and asking AniDB when the cache has nothing. For a file replacing another that is correct. For "record that I have this episode" it is destruction: an entry someone added from a different client, against a real file, would be removed to make room for a generic one carrying less information.

**It costs about four requests per episode.** A probe, a possible delete, the add, and then a follow-up read to recover the identifier the add does not return. A twenty-four episode season is on the order of eighty requests against an API that bans clients by IP for asking too often. The protocol needs one packet per episode.

There is also a hazard in the command itself that any entry point has to answer for. `MYLISTADD` overloads its episode number: absent or zero means *every episode of the anime*, and a negative number means *every episode up to that one*. An unset variable upstream therefore does not fail quietly — it writes several hundred entries into someone's list, and AniDB offers no bulk undo.

## Decision

**A file-less add is `Episode.add_to_mylist()`, a separate method that adds and only adds.**

- **It sends no `edit` parameter.** That makes "never overwrite an existing entry" a property of the protocol rather than a promise of this code: AniDB answers `310 FILE ALREADY IN MYLIST` and leaves the entry exactly as it was, whoever created it. Repeating the call is therefore harmless by construction, which is what a caller retried after a crash needs.
- **One request per call.** No probe before it, no read after it.
- **It reports one of three outcomes** — added, already present, rejected — carrying AniDB's own response code and text. "Already there" is a result of asking for something that exists, not a failure, and not the same thing as having created it.
- **It is per episode, not per season.** The caller writes the loop.
- **It refuses an episode number that does not name exactly one episode**, and refuses a mylist state that is not one of the four, before either can reach the wire.
- **It does not write the local cache.**

## Rejected alternatives

**A flag or a new parameter on `File.update_mylist()`.** The natural-looking option, and it keeps the exact thing that makes the method unusable here: the destructive probe is the method's contract, not an implementation detail, and a flag that turns it off produces one method with two contracts. The two operations differ in what they are *for* — one reconciles a file into a list, the other records an episode — and the safety property that matters here is best stated by a method that has no removal in it at all.

**A per-season batch, `Anime.add_to_mylist(episodes, …)`.** It matches how a caller thinks and it fails badly at the only moment it matters. A mylist write that cannot reach AniDB raises (SPEC-004), so a ban twenty episodes into a season discards the record of the nineteen that landed — the caller cannot tell what happened and must re-send the lot into an API that has just said stop. The caller's own loop holds that progress for free. A generator was considered and rejected for a worse reason: a write API that does nothing until it is iterated is a footgun, not a convenience.

**Writing a cache row from a successful add.** There is nothing faithful to write. AniDB answers a generic add with a count of entries created and no identifier for them, and this library's cached mylist rows are built around that identifier — `in_mylist` is defined as "a row with an lid", and every reader of those rows skips one without. A row written from a bare success would be a record the library invented rather than one AniDB confirmed, and invisible to everything that reads them.

**Spending a second request to recover the entry and cache it properly.** `MYLIST aid=…&epno=…` returns the real entry, and caching it is what the file-backed path already does after an add. This is deferred, not rejected: it doubles the metered cost of the operation for a consistency the only current caller does not read back, and it can be added later without changing this method's shape or its result type. See the consequences below for the inconsistency that leaves.

**Expanding a ranged episode number into several adds.** AniDB records one file covering several episodes as a single ranged epno, so an `Episode` can legitimately carry `5-7`, and the existing generic path in `File.update_mylist()` does expand ranges. Doing so here would make one call write three entries — the same "did more than I asked" the episode-number guard exists to prevent — and the API does not define what `MYLISTADD` makes of a range in any case, so passing it through would be shipping undefined behaviour.

## Consequences

**A caller's local cache does not know what it just added.** `Episode.in_mylist` keeps answering `False` for an episode added this way until something refreshes it from AniDB. This is a real inconsistency with the rest of the library, entered deliberately: it is the cheapest correct thing, it costs nothing that the current caller reads, and the request that would close it is named above and can be added without a breaking change. It is documented in SPEC-004 rather than left to be discovered.

**Two mylist add paths now differ in how strictly they read their arguments.** `add_to_mylist()` refuses an unrecognised mylist state; `File.update_mylist()` still drops one silently and lets AniDB apply its default. The silent drop is worth fixing and is not fixed here — it changes the behaviour of a method callers already use, which wants its own change and its own line in the release notes rather than riding along with a new feature.

**The library gains a mylist operation that never removes anything.** That is the point, and it is worth stating as a property to preserve: anything added to this path later that can delete an entry breaks the guarantee callers are being asked to rely on.
