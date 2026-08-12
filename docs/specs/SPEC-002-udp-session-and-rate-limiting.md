---
title: "UDP Session and Rate Limiting"
description: "Behavioral expectations for anidb-client's AniDB UDP transport: session establishment with optional AES-encrypted sessions, the sender and listener thread pair, the order they are started in, the sender waiting to be woken rather than polling, a callback thread per reply, and the shared state they reach through locked accessors, tag-based correlation of replies to commands over an unordered datagram protocol, monotonic timing for every interval it measures, outbound pacing (a short burst then a slower steady rate) that keeps a client from being IP-banned, exponential ban back-off with a ceiling, session-loss detection and re-authentication, command timeout and retry, NAT keepalive, and the rule that a library never terminates its host process."
status: accepted
tags: [udp, transport, session, authentication, encryption, aes, rate-limiting, pacing, ban, backoff, flood-protection, tags, correlation, timeout, retry, reauthentication, nat, keepalive, threads, thread-safety, locking, shared-state, thread-lifecycle, send-queue, monotonic-clock, logout, response-codes]
---

# UDP Session and Rate Limiting

AniDB's UDP API is unusually unforgiving: it is an unordered datagram protocol with a session, and the penalty for talking to it too fast is a temporary IP ban rather than an error reply. This spec describes how the library holds a session open, matches replies to commands, and paces itself so that a caller cannot get itself banned by accident.

## The session

A session is opened lazily and held for the life of the client. Authentication sends the account credentials together with a **client identity** — a registered name and integer version pair. AniDB refuses to authenticate an identity it does not know, so an application embedding this library registers its own and supplies it at init time (SPEC-006). The identity is held per transport rather than read from module state at send time, so an embedding application authenticates as itself without mutating global state.

When an encryption key is configured the session is established in two steps: an encryption handshake first, then authentication inside the resulting AES-encrypted channel. A configured key makes encryption mandatory — an attempt to authenticate unencrypted while a key is set is refused rather than silently falling back to plaintext.

Only a small set of commands may travel before a session exists — authentication, the encryption handshake, and the keepalive. Any other command sent without a session is refused rather than being sent and rejected by the server.

Closing the client logs out cleanly, waiting up to the transport timeout for the server to acknowledge. A client that was never authenticated simply closes its socket.

### Keeping the session alive

Two idle-driven behaviors keep a session usable:

- When the authenticated address AniDB reports back differs from the local port, the client is behind NAT and sends a periodic keepalive to hold the mapping open.
- After a long idle period the client sends a harmless command to refresh the session rather than discovering on the next real request that it has expired.

### Losing the session

The server can end a session at any time — through expiry, through an encryption session timing out, or by returning a code that says the session is no longer valid. Every one of those is handled the same way: re-authenticate, then re-send the command that discovered the loss, at the front of the queue so the caller is not made to wait behind unrelated work.

The exception is a logout that discovers the session is already gone. There is nothing left to do, so the client simply shuts down.

## Correlating replies to commands

UDP guarantees no ordering, so a reply carries a tag naming the command it answers, and that tag is the only thing tying the two together. Tags cycle through a fixed range and are handed out under a lock, because commands are queued from several threads at once — the caller's thread, the sender, and the listener re-queueing after a loss. Two commands sharing a tag would cross one command's reply onto the other's callback.

A reply arriving with no tag at all is not a reply to anything: it is the server volunteering something, and in practice it is a ban notice. Those are read for their response code and acted on directly.

Each matched reply's callback runs on **its own thread**, so a slow callback cannot stall the receive loop behind it. That this does not grow without bound is a consequence of the pacing below: replies cannot arrive faster than commands go out. A fixed worker pool was considered and rejected — a callback that waits on another reply would deadlock against a bounded pool, and the unbounded form has no such failure.

## Shared state across threads

The transport runs two threads of its own — one sending, one listening — and is called from whatever thread the application uses. Every piece of state written by one and read by another is reached through an accessor that takes a lock, rather than being touched directly.

- **The table of commands awaiting a reply**, inserted into by the sender and read, popped and iterated by the listener. Iterating a dict another thread is inserting into raises, and raising here ends the listener — after which nothing reads the socket and every caller waits on a reply that can no longer arrive.
- **The session key**, written when authentication succeeds and cleared when the session is lost. Code that tests it and then uses it must read it *once*: testing that a session exists and then reading it again to authorize a command lets the session be cleared in between, and the command goes out authorized with nothing.
- **The cipher** for an encrypted session, established by the encryption handshake and dropped with the session. It is cleared in the same critical section as the session key, because a half-cleared pair is either a command encrypted with a key the server has forgotten, or an unencrypted command on a session that requires one.
- **The pacing counters** — the burst allowance, the last-send time and the ban multiplier — every one of which is read-modify-write, and which the sender and the listener touch at the same time.

That last lock is **never held across a sleep**. A ban back-off is measured in half-hours, and holding the lock for the length of one would stop the listener being able to report the next ban at all.

The sender **does not poll for work**. It waits to be woken when a command is queued. It does wake on a timeout as well, but only because the idle keepalives above are driven by "has enough time passed", which no notification can express.

The listener is started by the transport **once the transport has finished constructing itself**, not from the listener's own constructor. The listener calls back into the transport — to report a ban, to trigger re-authentication, to re-queue a command — so starting it any earlier exposes it to attributes that do not exist yet, on a thread where the resulting failure is invisible and fatal.

## Pacing

The pacing policy is the thing that keeps a client out of trouble, so it is stated here explicitly rather than being inferable from whether a request happened to go out.

- A short opening burst of commands is allowed at a faster interval; after that burst is spent, a slower steady interval applies. This mirrors AniDB's documented flood protection, which permits a burst and then requires roughly one command every four seconds.
- Time the application spent on its own work counts toward the interval. An application that is slow in its own right is not paced twice.
- After a long enough quiet period the burst allowance is considered fresh again, because the server's own flood counter has decayed by then too.

A caller cannot send faster by asking. There is no override.

## Backing off

A ban, a server-busy reply, or a network send failure puts the transport into a backed-off state. The back-off starts at roughly the length of an AniDB temporary ban — retrying sooner is pointless — and doubles for each consecutive ban, up to a ceiling.

The ceiling exists because the doubling compounds when it is *authentication itself* that keeps failing. Unbounded, that sequence walks off into delays measured in days: a client that has, for practical purposes, stopped, while reporting only that it is waiting. In the ordinary banned-then-readmitted cycle the multiplier rarely leaves its first step, because a successful authentication clears the back-off entirely.

## Timeouts and retries

Every command in flight is watched. When one goes unanswered past the transport timeout:

- If replies from the server have arrived more recently than this command was sent, the API is alive and something else is happening — most likely a re-authentication in progress. The command is re-queued at the front.
- Otherwise the command has genuinely timed out. It is retried a bounded number of times, and once those are spent the transport treats the API as unavailable and backs off before trying again.

Every interval the transport measures — how long a command has been in flight, how recently a reply arrived, how long to pace or back off — is read from a **monotonic** clock rather than the wall clock. The wall clock can be stepped by a time sync or an operator: a backward step makes every command in flight look newly sent, and a forward step expires them all at once.

Authentication and the encryption handshake do not retry this way. A timeout on either means the API is not answering at all, so the transport backs off immediately rather than re-sending credentials into silence.

## Failure containment

Two rules bound how badly the transport may fail, both learned the hard way:

**A library never terminates its host process.** An unparsable reply, an unrecognised untagged response, or a failed cache fetch is logged and moved past. Neither the transport nor anything below it exits the interpreter — and in a non-main thread an attempt to do so would end only that thread, leaving the socket unread and every caller waiting on a reply that can no longer arrive.

**A waiter is always released.** Every path that can end a fetch — success, a not-found reply, an unguessable object, an exception mid-parse — sets the completion event that the calling thread is blocked on. A code path that returns early without doing so is a permanent hang, not a lost result.

## Related Artifacts

- **Line of truth (external):** the AniDB UDP API definition, which owns the command grammar, response codes, and the flood-protection thresholds this spec paces against. `src/anidb_client/commands.py` transcribes the command set and its parameter-combination rules; `src/anidb_client/responses.py` transcribes the response-code table.
- **Related specs:** SPEC-001 (the object layer whose attribute reads trigger these fetches); SPEC-003 (the cache that exists to keep this transport idle); SPEC-006 (credentials, the encryption key, the client identity and the outgoing port).
- **Tests:** pacing and back-off are tested against an injected clock in `tests/unit/test_ratelimit.py`, so a banned-state test does not block for the real back-off; the same file pins that the pacing lock is not held across that sleep, by having the injected sleep stand in for the back-off while another thread reports the next ban. Thread safety and thread lifecycle are covered in `tests/integration/test_link.py`: `TestSharedTransportState` for the locked session and cipher accessors and the fact that they are cleared together; `TestListenerStartup` for the listener not starting itself and the transport starting it once built; `TestSenderWakeup` for a queued command being dispatched without waiting for the idle tick, timed across enough round trips that a polling sender would be unmistakable; and `TestCallbackIsolation` for a blocked callback not stopping the next reply being read. Command construction and its parameter validation live in `tests/unit/test_commands.py`, response parsing and the code table in `tests/unit/test_responses.py` and `tests/unit/test_response_field_selection.py`. End-to-end session behavior runs against the fake server in `tests/fake_anidb.py` from `tests/integration/test_link.py`. The HTTP-side timeouts are covered in `tests/unit/test_http_timeouts.py`. The waiter-release guarantee is pinned from the response-handling side in `tests/unit/test_file_response_decoding.py`, where a reply that parses but cannot be decoded must still release everything waiting on it.
