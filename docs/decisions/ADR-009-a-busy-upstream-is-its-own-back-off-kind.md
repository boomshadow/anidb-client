---
title: "A Busy Upstream Is Its Own Back-Off Kind"
description: "anidb-client separates the back-off a busy AniDB asks for from the back-off a ban imposes, carrying the distinction as BackOffKind — an axis of its own, beside BanCause rather than inside it — on the rate limiter, on the health surface, on AniDBBannedError and in the limiter's resumable state, and choosing the base delay from it so a 602/601/604/600 waits half a minute where a 555/504 waits half an hour. Adding a BUSY member to BanCause was rejected because that enum answers how a back-off arose, not which refusal it was, and one value cannot answer both; deriving the schedule at each call site from disposition_for() was rejected because the standing back-off, not the reply, is what an embedder and a resumed process read; a second multiplier per kind was rejected because it adds a resumable dimension consumers would have to learn in order not to lose it; removing the back-off from a busy reply was rejected because pushing through a 602 is how a busy minute becomes a real ban; and reading the response code back off the error was rejected because a silent or local back-off carries no code."
status: accepted
tags: [udp, transport, rate-limiting, backoff, ban, server-busy, back-off-kind, ban-cause, disposition, response-codes, health-surface, durable-state, resume, additive-change, api-compatibility, error-data, orthogonal-axes]
---

# A Busy Upstream Is Its Own Back-Off Kind

## Context

AniDB answers a client it has had enough of with `555 BANNED` or `504 CLIENT BANNED`, and a client it simply cannot serve right now with `602 SERVER BUSY`, `601 ANIDB OUT OF SERVICE`, `604 TIMEOUT` or `600 INTERNAL SERVER ERROR`. The response table in `responses.py` transcribes that difference: the first pair carries the disposition `BANNED`, the second set `BACK_OFF`.

The transport computed that distinction on every refusal and then dropped it. Both dispositions closed the same gate through the same call, which opened a window starting at the length of an AniDB temporary ban and doubling per consecutive refusal. An embedding application doing a slow, well-paced walk of the catalogue drew a `602` after seven metered commands in ninety minutes — the upstream's own load, on any reading — and lost nineteen minutes to it, with the multiplier climbing towards the four-hour ceiling. Nothing the client had done could have earned that, and nothing it could have read would have told it so.

The second half of the problem is what a caller could see. `AniDBBannedError` carries `rescode`, so a caller holding the error *could* separate `602` from `555` by reading AniDB's table by hand. The transport's health surface could not: `is_banned`, `ban_cause`, `ban_remaining` and `ban_multiplier` report a `602` and a `555` identically, because `BanCause.REFUSED` is what both produce. So an application whose request path raised "busy" served a status endpoint that said "banned" about the same window — and a process that restarted, resuming the window and the multiplier from storage, had never seen the error at all and could only call it a ban.

`BanCause` is not the place to fix that, and its own docstring says why. It answers *how* the back-off arose — AniDB refused this client, AniDB said nothing, the datagram never left this host — which is a question about the path, not about the verdict. A `602` and a `555` arrive by the same path.

## Decision

**The kind of a back-off is a second axis, named `BackOffKind`, with the members `BANNED` and `BUSY`.** It is carried beside `BanCause` everywhere the cause already goes: on the rate limiter as a seedable field with a public reader, on `AniDBLink` as `back_off_kind`, and on `AniDBBannedError` as `kind`. It is chosen from the disposition the response table already computes, so `602`/`601`/`604`/`600` are `BUSY` and `555`/`504` are `BANNED`, and it is the axis the schedule is drawn from.

**The two schedules differ in what they count from, not in how they count.** A ban starts at `BAN_BASE_DELAY` (half an hour, the length of an AniDB temporary ban); a busy upstream starts at `BUSY_BASE_DELAY` (thirty seconds). Both double per consecutive refusal, both stop at `MAX_BAN_MULTIPLIER`, and both are jittered by the same floor-plus-remainder rule — so the longest a busy upstream can cost is four minutes against a ban's four hours. **A change of kind restarts the doubling**, because the multiplier counts a run of one kind of trouble and a busy reply is not the next step of a ban's escalation.

**A back-off opened with no verdict from AniDB to read takes the punishing schedule.** Silence and a local send failure both default to `BANNED`, and silence needs saying explicitly: it reaches the handshake path as a synthetic `604`, whose disposition is `BACK_OFF`, so read literally it would buy AniDB's *primary* enforcement mechanism a thirty-second window. The transport already refuses to hand that stand-in code on to a caller as something AniDB sent; it refuses to let it choose the schedule for the same reason.

**The change is additive.** `is_banned` still answers "a back-off stands", `ban_cause` still answers `REFUSED`, `ban_remaining`, `ban_multiplier`, `session_age` and `reported_address` are untouched, and the new limiter seed defaults so that a limiter constructed without it resumes exactly as before. A consumer that never learns the new field keeps the behaviour it had, minus the nineteen minutes.

Five alternatives were considered and rejected.

**Add a `BUSY` member to `BanCause`.** The smallest diff, and the one that destroys the thing it touches. `BanCause` answers how the back-off arose and has three answers that partition that question completely; `BUSY` is an answer to a different question, and a refusal that is both `REFUSED` and `BUSY` would have to pick one to report. The cost lands on the existing consumer, not on the new one: an application branching on `REFUSED` to mean "AniDB answered us" silently stops seeing the busy case, which is the majority of what it was branching on. Two questions, two fields.

**Derive the schedule at each call site from `disposition_for()` and store nothing.** Tempting, because the disposition is already in hand where the back-off is opened. It fixes the timing and leaves the reporting exactly as broken as it was: the window outlives the reply that opened it, and the two callers who need the answer — a status endpoint reading the live surface, and a process resuming a stored window — are precisely the two who do not have the reply. Requirement one of the incident was that the *standing* back-off be readable.

**Give each kind its own multiplier.** The most obviously correct-looking version, and the one that quietly breaks the seed/resume contract. Every resumable field has to be captured as well as seeded, so a second multiplier is a second thing every consumer must learn to store; a consumer that stores only `ban_multiplier`, as `anidb-sluice` does, silently drops the other one on every restart and resumes a back-off that has forgotten how far it had escalated. One multiplier that restarts when the kind changes gives the same behaviour on the sequences that occur, and adds one field to persist rather than two.

**Give the busy schedule its own ceiling.** Rejected as a constant that would earn nothing. The kinds already differ by a factor of sixty in their base delay, so the shared ceiling bounds the busy schedule at four minutes without help — and a second ceiling is a second value to seed-validate against, keep in step, and explain.

**Stop backing off from a busy reply.** Rejected outright, and worth recording so it is not proposed as the simple fix. Exponential back-off on a busy upstream is the standard and correct behaviour, and against this service in particular it is load-bearing: hammering through a `602` is a well-trodden way to turn a busy minute into a real ban, which is the outcome every rule in SPEC-002 exists to avoid. The defect was never that the client backed off. It was that it served a punishment for someone else's bad minute.

## Consequences

**A busy upstream costs seconds, and a ban costs what it always did.** The schedule an application experiences during an AniDB load spike is now proportionate to the spike, and the schedule it experiences during a ban is unchanged — which matters, because that one was never wrong.

**An embedding application can tell the two apart from live state.** `back_off_kind` answers on the transport, on the limiter and on the error, so a request path and a status endpoint describe one back-off the same way, and a process that restarts into a resumed window describes it the same way the process that opened it did. The distinction survives storage because it is one of the fields a limiter may be seeded with.

**Consumers that store back-off state have one more field to store.** Not storing it is safe and silent: an omitted kind resumes as a ban, which over-waits rather than under-waits. The cost of ignoring the new field is bounded by the behaviour that existed before it.

**`BanCause` keeps its meaning, and there is now a stated place for the next distinction.** The two axes are named and separated, so a future question about a refusal has an obvious form — is this about how the back-off arose, or about what it is? — rather than a fourth member wedged into whichever enum is nearest.
