---
title: "File Identification and Mylist Management"
description: "Behavioral expectations for identifying local files against AniDB and managing mylist entries: ed2k hash plus size as the identity AniDB recognises, the inference fallback when AniDB has never seen a file (episode number guessed from the filename by an ordered regex ladder, anime guessed from the parent directory then the filename), generic entries for unknown files, multi-episode files, and the add/edit/remove semantics of update_mylist() and remove_from_mylist() including the one-entry-per-episode rule, a write the transport cannot deliver raising on a bounded wait rather than reporting a change that never happened, and the promotion of a generic entry once AniDB learns the real file; and the file-less add for a caller who has only an anime id and an episode number, which is its own entry point because the one-entry-per-episode rule reaches its state by removing — an add that sends no edit and so cannot overwrite whatever entry is already there, costs one request with no probe and no read after it, answers added, already present or rejected rather than raising on AniDB's no, refuses locally an episode number that does not name exactly one episode and a state outside the vocabulary, and writes no cached row because AniDB returns no identifier to write one against."
status: accepted
tags: [file-identification, ed2k, hashing, md4, mylist, mylistadd, mylistdel, generic-file, file-less-add, additive, idempotent, inference, filename-parsing, episode-number, specials, openings, endings, trailers, multi-episode, roman-numerals, nfs, watched-state, promotion, timeout, error-reporting]
---

# File Identification and Mylist Management

The typical use of this library is mylist management: pointing it at files on disk and having them added to, edited in, or removed from an AniDB mylist. That requires answering a question AniDB itself often cannot: *which episode is this file?*

## Identifying a file

AniDB identifies a file by its **size together with its ed2k hash** — not by name, and not by content beyond that hash. Given a local path, the library computes both and asks AniDB. Hashing follows the ed2k definition, including its degenerate cases: a file short enough to be a single chunk hashes as that chunk, and an empty file hashes as the hash of nothing rather than failing.

Hashing is expensive, so a cached hash is reused when the file's size and modification time both still match what was cached. Any difference and the file is re-hashed.

Paths may be local or, when the optional NFS support is installed, `nfs://` URLs. Reading and stat-ing go through the same two operations either way, so nothing above this level distinguishes them.

## When AniDB has never seen the file

A file AniDB does not recognise is not a failure. The library infers what the file contains and records a **generic entry** — a cached row describing an anime and an episode with no file identity of its own, marked as generic. This is the case that matters most in practice: personal encodes, unindexed releases, and anything newer than AniDB's own coverage.

Inference proceeds in two independent parts.

### Which anime

The parent directory name is tried first, matched against the anime-titles data (SPEC-005). Directory names are usually close to a real title, so this is the reliable signal and it is used at the normal matching threshold.

Failing that, the filename is tried, but only after being stripped down: bracketed, parenthesised and braced segments (group tags, codec notes, CRCs) are removed, episode-number patterns are removed, the extension is dropped, and what remains is reduced to its words. What is left is a poor approximation of a title, so it is matched at a deliberately lower threshold — a confident match against a mangled string is not available, and demanding one would reject every file.

If neither yields a match the file cannot be identified, and the object becomes illegal (SPEC-001) rather than being attached to a wrong anime.

Directory parsing can be turned off per file, and a caller who already knows the anime can supply it, in which case only the episode is inferred.

### Which episode

The episode number is matched by an **ordered ladder of patterns**, and the order is the design. Earlier patterns are specific and unambiguous — `S01E02`, `ep01`, `1x09`, an explicit specials marker, a dash-delimited number. The ladder then reaches a marked breakpoint, past which the patterns are fallbacks loose enough to match almost anything, ending with "the first number in the name".

The fallbacks are only tried after a decisive test in between: **if the anime has exactly one episode, that episode is the answer.** A single-episode anime is a movie or an OVA, and running a greedy number-matcher over its filename is far more likely to pick up a year, a resolution or a version number than an episode number. A caller may also force this single-episode assumption explicitly.

Matched numbers are normalised into AniDB's episode vocabulary: specials, openings, endings, trailers and credits each carry their own prefix rather than being numbered alongside regular episodes. Endings are placed by assuming they start halfway through the credit count, which is a guess and is documented as one. A number expressed as a Roman numeral is converted; a number that is neither numeric nor a numeral is skipped with a warning rather than aborting the match.

### Multi-episode files

A filename can name a range, and the library expands it: two endpoints of the same kind become every episode between them. Where a range is found in the filename, it is trusted only if it contains the episode the cache already believes the file to be — otherwise the filename is assumed wrong and the cached episode stands alone.

A cached episode number can itself be a range, which is how AniDB records a single file covering several episodes, and that is expanded the same way. However a file's episode set is arrived at — from a range in the filename, from a ranged episode number, or from the single episode it turns out to be — it is a list of episode **numbers as text**, the same form `Episode.episode_number` carries and the same form the mylist commands put on the wire. Containment (SPEC-001) and both mylist loops read that set, so a representation that differed by route would make them disagree about the same file.

The two loops do not read it by the same route, and the difference is deliberate. **Removal expands a ranged episode number and stops there**; it does not consult the filename. Expanding the range is what makes removal symmetric with adding — the defect it fixes is a ranged file that was added as several entries and removed as one, against an episode number no mylist entry carries, so the removal reported success and removed nothing. Adopting the filename's episode set as well is a different thing: it would let a filename decide what is deleted from someone's mylist, which a destructive path should not acquire as a side effect.

Multi-episode support is genuinely partial, and the boundary is worth stating: **filename parsing supports it, the AniDB file API does not.** A real file in AniDB covering several episodes reports one, and the episode set is not cached. It is reliable for generic entries, where the library owns the whole record, and unreliable for files AniDB knows.

A file may also be a *part* of an episode rather than a whole one, detected from the filename and recorded so that external-id mapping (SPEC-005) can distinguish "part 2 of a movie" from "episode 2 of a series".

## Mylist operations

`update_mylist()` both adds and edits — which of the two happens depends on whether a mylist entry already exists, not on which method the caller calls. It accepts the mylist state (`unknown`, `on hdd`, `on cd`, `deleted`), a watched flag that may instead be the datetime the file was watched, and free-form source and other fields.

**One entry per episode.** Before adding, the library ensures no other entry already covers this episode — checking the local cache first and only asking AniDB when the cache has nothing — and removes what it finds. AniDB permits several mylist entries for one episode; this library does not support that arrangement and says so rather than silently creating a second entry.

**Generic files add per episode.** A generic entry covering several episodes sends one add per episode. A real file sends one, identified by its file id where known and by size and hash otherwise.

**Editing is local-first.** An edit that AniDB accepts updates the cached row directly from what the caller asked for. An add cannot, because the API does not return the new entry's identifiers, so an add is followed by a mylist read to pick them up.

`remove_from_mylist()` removes by whichever identifier the entry carries — file id, mylist id, or, for a generic entry, one removal per episode by anime and episode number. The cached mylist fields are cleared regardless of what AniDB answered: a removal that reports the entry was not there anyway has still reached the intended state.

**A mylist operation that cannot reach AniDB raises, and waits a bounded time before it does.** Both methods block until the change has been acknowledged, and both are subject to the rule SPEC-001 states for attribute reads: a request the transport cannot deliver — banned, unanswered, refused — surfaces as an exception naming the reason rather than as silence. That matters more here than for a read. A read that quietly fails costs a stale value; a mylist write that quietly fails leaves the caller believing it changed someone's list when it did not. A rejection *by AniDB* is a different thing and is still logged rather than raised: the request arrived and was answered, and the answer was no.

### Adding an episode with no file at all

Everything above starts from a file. A caller that has no usable one — a pipeline that re-encodes what it keeps, so the hash will never match anything AniDB holds — has only an anime id and an episode number, and that is enough for AniDB: `MYLISTADD` accepts an anime, a `generic` marker and an episode number, and creates the same file-less entry the web form's *Add To My List* button does.

`Episode.add_to_mylist()` is that operation, and it is a **different operation from `update_mylist()`, not a mode of it**. The difference is the one-entry-per-episode rule above: that rule is enforced by *removing* whatever entry already covers the episode, which is correct for a file replacing another and destructive for a caller who only means to record what they have. ADR-006 is why it is a separate entry point.

**It adds, and it is incapable of doing anything else.** The command carries no `edit`, so an episode that already has an entry is answered by AniDB with "already in mylist" and the existing entry is left exactly as it was — including one added from another client, against a real file. That is a property of the protocol rather than a promise of the code, which is what makes calling it twice harmless and makes retrying after a crash the ordinary way to finish a partial run.

**One request per episode**, with no probe before it and no read after it. The caller writes the loop over a season; the library does not offer a batch, because a mylist write that cannot reach AniDB raises, and a batch that raises part-way through discards the record of the episodes that had already landed.

**The answer is one of three things**, and they are not two: *added*, *already present*, and *rejected*, the last carrying AniDB's own response code and text so that "no such anime" and "no such episode" stay distinguishable. Rejection is a returned result, not an exception — the same line the rest of this section draws, and drawn for the same reason.

Two arguments are refused locally, before anything is sent. **An episode number that does not name exactly one episode**, because `MYLISTADD` overloads that field: a missing or zero number means every episode of the anime and a negative one means every episode up to it, so an unset variable upstream would write hundreds of entries into a list that has no bulk undo. A ranged number is refused with them — AniDB does not define what this command makes of one, and expanding it here would make a single call write several entries. And **a mylist state that is not one of the four**, because a name that matches nothing would otherwise be dropped from the command and the entry filed under AniDB's default, telling the caller a state had been recorded when a different one had.

**The local cache is not written, and a caller feels it.** AniDB answers a generic add with a count of entries created and no identifier for them; this library's cached mylist rows are built around that identifier, and a row without one is invisible to every reader of them. So there is nothing faithful to write, and `in_mylist` does not know about the entry until something refreshes it from AniDB. Closing that gap costs a second request per episode to read the entry back, which is a deliberate deferral rather than an oversight — ADR-006 records it as the next iteration.

### Promotion

A file that was generic and that AniDB later recognises is **promoted**. The old generic mylist entry is removed and a real one added in its place, carrying across the state, watched date, source and other fields so the promotion is invisible in the caller's mylist. This is the expected lifecycle for a file added before AniDB indexed it.

## Related Artifacts

- **Line of truth (self-enforcing):** `src/anidb_client/db.py` — the file table, including the generic marker and the constrained mylist state and file-state vocabularies.
- **Line of truth (external):** the AniDB UDP API's `FILE`, `MYLIST`, `MYLISTADD` and `MYLISTDEL` commands and their accepted parameter combinations, transcribed in `src/anidb_client/commands.py`.
- **Related decisions:** ADR-006 (why the file-less add is its own entry point rather than a mode of `update_mylist()`, why there is no per-season batch, and why the cached row is deferred).
- **Related specs:** SPEC-001 (`File` construction and the illegal-object outcome); SPEC-003 (how an identified file is cached and re-validated); SPEC-005 (title matching, which anime inference depends on, and the part-vs-episode distinction in external mapping); SPEC-002 (the pacing every one of these commands is subject to).
- **Tests:** ed2k hashing and file stats in `tests/unit/test_fileinfo.py`; the regex ladder itself in `tests/unit/test_filename_inference.py`; end-to-end inference from a filename to an `Episode` in `tests/unit/test_episode_from_filename.py`; mylist add, edit, removal and promotion in `tests/unit/test_mylist.py`, where the two per-episode loops are pinned as a mirrored pair — a ranged file adds and removes one entry per episode, and a path-backed file's removal is unchanged by its filename. `TestMylistWritesThatCannotReachAniDB` in the same file covers the other side of that: an add or a removal the transport cannot deliver raises rather than reporting success or hanging, the per-episode removal loop stops on the first failure instead of sending the rest into an API that has said stop, and a rejection *by* AniDB is still not an error. `TestAddingAGenericEntryWithoutAFile` covers the file-less add: the whole command asserted whole so that the absent `edit` is pinned as much as the present `generic`, the single request with nothing else on the wire, the three outcomes, the two arguments refused before anything is sent, and the deliberately unwritten cache; file identity and equality in `tests/unit/test_file_identity.py`; the reply that carries the entry AniDB already holds in `tests/unit/test_responses.py`.
