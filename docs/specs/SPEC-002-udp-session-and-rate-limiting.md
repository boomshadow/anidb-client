---
title: "UDP Session and Rate Limiting"
description: "Behavioral expectations for anidb-client's AniDB UDP transport: session establishment with optional AES-encrypted sessions, a handshake whose success is recognised by whitelist and which always settles — refused credentials latched and never re-sent, a ban retried after a back-off, the sender and listener thread pair, the order they are started in, the sender waiting to be woken rather than polling, a callback thread per reply, and the shared state they reach through locked accessors, tag-based correlation of replies to commands over an unordered datagram protocol, untagged replies classified from the response-code table rather than from a list restated in the transport, monotonic timing for every interval it measures, outbound pacing (a short burst then a slower steady rate) that keeps a client from being IP-banned, exponential ban back-off with a ceiling, session-loss detection and re-authentication, a command timeout whose retry budget is spent rather than renewed, every request carrying an outcome — the reply or the reason there will not be one — and a transport that tells every caller when it can no longer work, NAT keepalive, and the rule that a library never terminates its host process."
status: accepted
tags: [udp, transport, session, authentication, encryption, aes, rate-limiting, pacing, ban, backoff, flood-protection, tags, correlation, timeout, retry, reauthentication, nat, keepalive, threads, thread-safety, locking, shared-state, thread-lifecycle, send-queue, monotonic-clock, logout, response-codes, disposition]
---

# UDP Session and Rate Limiting

AniDB's UDP API is unusually unforgiving: it is an unordered datagram protocol with a session, and the penalty for talking to it too fast is a temporary IP ban rather than an error reply. This spec describes how the library holds a session open, matches replies to commands, and paces itself so that a caller cannot get itself banned by accident.

## The session

A session is opened lazily and held for the life of the client. Authentication sends the account credentials together with a **client identity** — a registered name and integer version pair. AniDB refuses to authenticate an identity it does not know, so an application embedding this library registers its own and supplies it at init time (SPEC-006). The identity is held per transport rather than read from module state at send time, so an embedding application authenticates as itself without mutating global state.

When an encryption key is configured the session is established in two steps: an encryption handshake first, then authentication inside the resulting AES-encrypted channel. A configured key makes encryption mandatory — an attempt to authenticate unencrypted while a key is set is refused rather than silently falling back to plaintext.

Only a small set of commands may travel before a session exists — authentication, the encryption handshake, and the keepalive. Any other command sent without a session is refused rather than being sent and rejected by the server.

### When authentication does not succeed

A handshake has two outcomes, not one. Treating it as a signal that can only ever say *it worked* is what turned a refusal into a hang: the sender waited on a flag that a refused login had no way to set.

**Success is recognised by a whitelist.** Only the codes that mean the handshake worked — and that carry the field the handshake produces, a session key or a salt — reach the handlers that record it. Anything else fails the attempt. The alternative, a list of the codes that mean failure, is the one that cannot be kept complete, and when it was incomplete a refusal fell through to the success handler and raised there, on a thread where nothing could see it.

**Every attempt settles.** Refused, unanswered, or answered with something unrecognisable — each ends the attempt and releases whoever waited on it. An attempt that ends without settling leaves the sender parked on a reply that has already been and gone, and leaves the client believing a handshake is still in flight, so every later attempt declines to start.

**Whether to try again is decided from the response code, and defaults to no.** A code that says the server is unhappy — busy, out of service, banning this client for now — is temporary, so it registers a back-off and a later attempt may be made. Everything else is a refusal of these credentials or this client identity: a wrong password, an unregistered client, an encryption type the server does not offer. Those are **latched**, and no further authentication is sent for the life of the client. Retrying credentials AniDB has already rejected cannot succeed, and is a good way to turn a refusal into a ban.

**A retryable failure is paid for on the sender's thread.** The listener reports the failure and returns to reading the socket; it does not start the next attempt, because starting one means sending, and sending means waiting out the back-off. The next command that needs a session drives the next handshake, and waits there — where waiting costs nothing. A client with nothing queued does not authenticate at all, least of all into an API that has just asked it to back off.

Closing the client logs out cleanly, waiting up to the transport timeout for the server to acknowledge. A client that was never authenticated simply closes its socket.

### Keeping the session alive

Two idle-driven behaviors keep a session usable:

- When the authenticated address AniDB reports back differs from the local port, the client is behind NAT and sends a periodic keepalive to hold the mapping open. That address is **advisory**: AniDB only returns it when authentication asked for it, so a reply that omits it, or carries something that is not an address and port, means NAT was not detected — not that the login failed. A login that succeeded on the wire must not be undone by the check that was only advising it.
- After a long idle period the client sends a harmless command to refresh the session rather than discovering on the next real request that it has expired.

### Losing the session

The server can end a session at any time — through expiry, through an encryption session timing out, or by returning a code that says the session is no longer valid. Every one of those is handled the same way: re-authenticate, then re-send the command that discovered the loss, at the front of the queue so the caller is not made to wait behind unrelated work.

The exception is a logout that discovers the session is already gone. There is nothing left to do, so the client simply shuts down.

## Correlating replies to commands

UDP guarantees no ordering, so a reply carries a tag naming the command it answers, and that tag is the only thing tying the two together. Tags cycle through a fixed range and are handed out under a lock, because commands are queued from several threads at once — the caller's thread, the sender, and the listener re-queueing after a loss. Two commands sharing a tag would cross one command's reply onto the other's callback.

A reply arriving with no tag at all is not a reply to anything: it is the server volunteering something, and in practice it is a ban notice. Those are read for their response code and acted on directly.

**Which codes those are is a property of the code table, not of the transport.** Every response code carries a *disposition* alongside its payload shape — ordinary, back off, or banned — recorded once beside the code it belongs to. The transport reads that disposition; it does not keep its own list. The transport used to restate the set as a literal tuple of integers, and the restatement disagreed with the table: the code AniDB actually answers with when it has had enough of a client was mapped to a response class and missing from the tuple, so the one reply that says *stop* was logged as unrecognised while the client kept sending. A classification that lives in one place cannot disagree with itself.

An untagged code the table does not know is logged and moved past. It is not guessed at, and in particular it is not assumed to be a ban.

Each matched reply's callback runs on **its own thread**, so a slow callback cannot stall the receive loop behind it. That this does not grow without bound is a consequence of the pacing below: replies cannot arrive faster than commands go out. A fixed worker pool was considered and rejected — a callback that waits on another reply would deadlock against a bounded pool, and the unbounded form has no such failure.

## Shared state across threads

The transport runs two threads of its own — one sending, one listening — and is called from whatever thread the application uses. Every piece of state written by one and read by another is reached through an accessor that takes a lock, rather than being touched directly.

- **The table of commands awaiting a reply**, inserted into by the sender and read, popped and iterated by the listener. Iterating a dict another thread is inserting into raises, and raising here ends the listener — after which nothing reads the socket and every caller waits on a reply that can no longer arrive.
- **The session key**, written when authentication succeeds and cleared when the session is lost. Code that tests it and then uses it must read it *once*: testing that a session exists and then reading it again to authorize a command lets the session be cleared in between, and the command goes out authorized with nothing.
- **The cipher** for an encrypted session, established by the encryption handshake and dropped with the session. It is cleared in the same critical section as the session key, because a half-cleared pair is either a command encrypted with a key the server has forgotten, or an unencrypted command on a session that requires one.
- **The handshake in flight**, created by whichever thread starts an attempt and completed by whichever thread learns its outcome — the listener on a reply, the listener's timeout sweep on silence. It is what makes *settled* distinguishable from *succeeded*: a flag can only report success, and this carries the failure too. It is taken and cleared inside the lock so that two threads cannot both settle the same attempt, and so a fresh attempt started by one thread is never completed by a reply belonging to the last one.
- **The latched authentication failure**, set once when a refusal is final and read before any attempt starts. It is the thing that stops the client re-offering credentials AniDB has already rejected, so a torn read of it is a client that keeps asking.
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
- If the command has no send time at all, it was re-queued while the sweep was running and has not gone out again. It has not timed out, so it is re-queued rather than retried — the sweep collects expired commands and claims them in two passes, and a re-queue landing between the two is expected rather than exceptional.
- Otherwise the command has genuinely timed out. It is retried, up to a fixed number of times *in total*.

**The budget is spent, not renewed.** When it runs out the command fails: whoever asked for it is told no reply is coming, and the transport backs off before anything else goes out. There is no branch that restores the allowance and tries again, because that is not a retry policy — it is an unbounded loop with a growing interval. It was the previous behaviour, and it is the reported hang: a command AniDB would never answer was re-sent for the life of the process while its caller waited on a signal that only success could send.

What the budget bounds is **how many times the command reaches AniDB**, not how many times it goes round the queue. A service that bans clients for asking too often counts requests.

The caller is told *before* the back-off starts, in that order. The decision to give up has already been made; delivering it after a back-off would hold the caller for half an hour or more to hand over an answer that was ready immediately.

Every interval the transport measures — how long a command has been in flight, how recently a reply arrived, how long to pace or back off — is read from a **monotonic** clock rather than the wall clock. The wall clock can be stepped by a time sync or an operator: a backward step makes every command in flight look newly sent, and a forward step expires them all at once.

Authentication and the encryption handshake do not retry this way. A timeout on either means the API is not answering at all, so the transport backs off immediately rather than re-sending credentials into silence — and it settles the handshake, because a sender waiting on an authentication is waiting on something that now has an answer, even if the answer is silence.

## Every request has an outcome

A command carries its result to whoever asked for it, and that result is *either* the reply *or* the reason there will not be one.

A callback cannot do this. There is no callback for a reply that never arrives, so a caller with only a callback cannot tell "AniDB says there is no such thing" from "we were banned and gave up" — and on the second it has nothing to wait for. Both are answers; only one of them was reaching the caller.

- A reply settles the command **after** its callback has run, so a caller released by the outcome finds the callback's work — the cache write — already visible.
- A callback that raises fails the command. A reply that arrived and was then mishandled is still no answer, and it used to be *less* visible than one that never came: the handler ran on a thread nobody joins, so the exception was invisible at the time and the caller waited anyway.
- Retrying reuses the same outcome. The caller is waiting on the request, not on any one attempt at it.
- **When the transport concludes it cannot work at all, that conclusion reaches every caller it was working for** — everything queued and everything awaiting a reply fails, and requests made afterwards fail immediately rather than joining a queue nothing will drain. A transport that stops silently is worse than one that stops loudly: every caller is left holding a request that can no longer be served and has no way to find out.

That last rule is what the listener-liveness check was always for. It read as *kill the main thread if the listener dies*, and what it actually did was raise on the **sender** thread, ending the one thread that could have told anybody. Both threads then being gone, the queue stopped draining, every command in it had no send time — so the timeout sweep skipped it — and every caller waited on a reply that could not be read even if it arrived.

## Failure containment

Two rules bound how badly the transport may fail, both learned the hard way:

**A library never terminates its host process.** An unparsable reply, an unrecognised untagged response, or a failed cache fetch is logged and moved past. Neither the transport nor anything below it exits the interpreter — and in a non-main thread an attempt to do so would end only that thread, leaving the socket unread and every caller waiting on a reply that can no longer arrive.

**A waiter is always released.** Every path that can end a fetch — success, a not-found reply, an unguessable object, an exception mid-parse, a refused or unanswered handshake — releases whoever is blocked on it. A code path that returns early without doing so is a permanent hang, not a lost result.

Releasing is not the same as succeeding, and the two must be distinguishable. A signal that can only say *done* cannot report a failure, so the waiter cannot tell "there is no answer" from "the answer never came" — which is why the handshake carries its outcome rather than a bare flag.

## Related Artifacts

- **Line of truth (external):** the AniDB UDP API definition, which owns the command grammar, response codes, and the flood-protection thresholds this spec paces against. `src/anidb_client/commands.py` transcribes the command set and its parameter-combination rules; `src/anidb_client/responses.py` transcribes the response-code table.
- **Related specs:** SPEC-001 (the object layer whose attribute reads trigger these fetches); SPEC-003 (the cache that exists to keep this transport idle); SPEC-006 (credentials, the encryption key, the client identity and the outgoing port).
- **Tests:** pacing and back-off are tested against an injected clock in `tests/unit/test_ratelimit.py`, so a banned-state test does not block for the real back-off; the same file pins that the pacing lock is not held across that sleep, by having the injected sleep stand in for the back-off while another thread reports the next ban. Thread safety and thread lifecycle are covered in `tests/integration/test_link.py`: `TestSharedTransportState` for the locked session and cipher accessors and the fact that they are cleared together, and for the two handshake items alongside them — two threads racing to settle the same attempt, where exactly one may win and the loser must find nothing to settle rather than a settled one; settling an attempt that has already gone; and the latched refusal being read under the lock, so no attempt starts after it; `TestListenerStartup` for the listener not starting itself and the transport starting it once built; `TestSenderWakeup` for a queued command being dispatched without waiting for the idle tick, timed across enough round trips that a polling sender would be unmistakable; `TestCallbackIsolation` for a blocked callback not stopping the next reply being read; and `TestListenerRobustness` for the listener surviving garbage, an unrecognised untagged code, and a command re-queued midway through the timeout sweep — the garbage case also pins settlement on that path, because surviving the packet is only half of it: the handshake the garbage answered has to settle too, or nothing can start another one. `TestCommandOutcome` in `tests/integration/test_link.py` covers the outcome a request carries: a reply settling the command that asked for it, the callback having finished before the outcome arrives, a command that is never answered failing its caller rather than hanging it, the bounded number of times such a command reaches the wire, and a callback that raises failing the command instead of stranding it. Command construction and its parameter validation live in `tests/unit/test_commands.py`, where `TestRetryPolicy` pins that the attempt budget is spent rather than renewed, that the caller is told before the back-off begins, and — driven through the same counting the transport does — that a command retried to exhaustion terminates after a fixed number of sends; response parsing and the code table in `tests/unit/test_responses.py` and `tests/unit/test_response_field_selection.py`. `TestDisposition` in `tests/unit/test_responses.py` pins the classification each code carries, including the whole set of codes that must stop the client, so losing one is a test failure rather than something only AniDB notices. `TestBanHandling` in `tests/integration/test_link.py` drives the codes that arrive *untagged* through the listener as real unsolicited replies; a code that only ever answers a command, and so arrives tagged, is not meaningful to test on that path. `TestFailedAuthentication` in the same file covers the tagged half: every code AniDB can refuse a handshake with settles rather than parking the sender, a rejected credential is never offered a second time, a ban is retryable where a rejection is not, and an unanswered handshake settles like a refused one. End-to-end session behavior runs against the fake server in `tests/fake_anidb.py` from `tests/integration/test_link.py`. The HTTP-side timeouts are covered in `tests/unit/test_http_timeouts.py`. The waiter-release guarantee is pinned from the response-handling side in `tests/unit/test_file_response_decoding.py`, where a reply that parses but cannot be decoded must still release everything waiting on it, and in `tests/unit/test_enum_converters.py`, where an ANIME or GROUP reply naming a relation the library cannot read must do the same.
