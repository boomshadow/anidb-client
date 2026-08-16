---
title: "The Relation Graph"
description: "Behavioral expectations for AniDB's anime relation graph and how this library hands it to a caller: relations as (relation_type, Anime) pairs whose type is AniDB's own encoding and is returned as data rather than applied as policy; the transitive walk, the edge type reported being the one an anime was first reached by rather than a description of the route, and the cyclic graph terminating by refusing to revisit; the walk bounded mechanically by a traversal budget and an optional depth rather than semantically by relation type, with relation-type and mylist filters offered to the caller who has a policy; a bounded answer that says it is bounded, so a truncated result never looks like a complete one; the ambiguity of the `other` relation and why no type filter is correct for everyone; and the measured shape of AniDB's real graph that the bound exists to survive."
status: accepted
tags: [relations, relation-graph, related-anime, traversal, walk, budget, depth, truncation, relation-types, sequel, prequel, side-story, other, franchise, rate-limiting, ban-avoidance, object-layer, public-api]
---

# The Relation Graph

AniDB records how its anime entries relate to one another — this is a sequel to that, this is a side story of that, this shares a setting with that. The graph those links form is the closest thing that exists to a machine-readable answer to "what else belongs to this show", and it is the community's collective judgment rather than any one person's recollection.

This spec covers what the library does with that graph. SPEC-001 covers the object layer the graph is read through; SPEC-003 covers the rows it is cached in; ADR-005 covers why the division of labour below is drawn where it is.

## Whose answer this is

**AniDB is the authority on what belongs to a show.** Not this library, not the application calling it, not the person running that application. The library's job is to make the graph easy to get, faithfully and completely. Deciding what a given caller means by "the same show" is the caller's job, and the library does not do it on their behalf.

That division has a practical consequence that governs everything below: **the library does not pre-filter the graph.** Filtering by relation type inside the library would decide relevance before anyone has looked at the evidence, and a caller who then needs an entry the filter excluded cannot recover it — the record was discarded before they saw it. Returning everything and letting the caller reconcile loses nothing; pre-filtering can only lose information it has not earned the right to discard.

The bounds the library *does* apply are mechanical rather than semantic: they cap how much work a walk may do, not which anime deserve to be in the answer.

## Reading one anime's relations

`Anime.relations` is a list of `(relation_type, Anime)` pairs. The relation type is AniDB's own vocabulary, transcribed into the cache's enumeration (SPEC-003) — `sequel`, `prequel`, `side story`, `parent story`, `same setting`, `alternative setting`, `alternative version`, `character`, `music video`, `summary`, `full story`, `other`.

Two things about that vocabulary are worth stating because both have been misread:

- The types are **data to be returned, never policy for the library to apply**. Reading them is deferring to AniDB; flattening them away — answering an undifferentiated set of anime — would discard a distinction the community deliberately drew, and is *less* faithful to the database than reporting them. Handing them to the caller is how both stay true at once.
- `character` is an **anime-to-anime** relation, meaning two entries that share characters. Relations never yield character records, or records of anything other than anime. There is no path by which a caller asking for relations receives an entity of another kind.

An anime the cache has never seen is resolved before its relations are read, by the ordinary fetch of SPEC-001. During a walk that is the normal case rather than the exception.

## Walking the graph

`Anime.related_anime()` walks those links transitively from one anime and reports what it reached. The result names the anime it started from, and gives every anime reached as the same `(relation_type, Anime)` pairs `relations` uses, so the two read alike.

**The type reported for an anime is the edge it was first reached by, not a description of the route.** An anime three hops away is reported with the type of the third hop; the two hops before it are not in the answer. This matters most when the walk is left unfiltered, where an entry reached `sequel → sequel` and one reached `other → sequel` arrive looking identical although only one of them is the same show. It largely dissolves for a caller who names the relation types to follow, since every edge on every route is then one they allowed.

The graph is cyclic by construction — every sequel link has a matching prequel link back — so the walk terminates by refusing to revisit an anime it has already reached or already queued, not by exhausting a depth.

If the walk reaches an id AniDB does not recognise, it stops and reports what it found rather than failing the whole traversal. A fetch that cannot be answered at all — banned, timed out, the transport gone — raises, as every other read does (SPEC-001): that is not a bounded answer, it is no answer.

## What bounds a walk

Four controls, and they are not the same kind of thing. Two are the library's own safety bound; two are the caller's policy, applied only when the caller states one.

**The traversal budget** is the safety bound and is on by default. It caps the number of anime a walk will reach. It exists because every anime reached may cost a request to an API that bans clients for asking too often, and because AniDB's graph contains components far larger than any caller means by "this show" — see the measurements below. The default is sized so an ordinary series and its side stories complete comfortably, and a franchise-scale component does not. A caller who wants a larger walk raises it deliberately.

**Depth** is optional and off by default. It bounds how far from the starting anime the walk travels, counting the starting anime's own relations as one.

**Relation types to follow** is optional, and following every type is the default. When the caller names a set, an anime reached only by a type outside it is neither returned nor traversed through. This is where a caller's semantic policy goes, and the library has none of its own to apply here.

**Mylist membership** is optional and off by default. When set, the walk follows only anime already in the caller's mylist. It is a use-case filter for someone cataloguing their own collection, not a safety bound — the budget is the safety bound, and this has no second role. It is off by default because for a caller who does not use mylist at all it rejects every neighbour, and a walk that returns only the anime it started from is not an answer.

`exclude` is separate from all four: an iterable of anime treated as walls, neither returned nor traversed through.

## A bounded answer says it is bounded

**A walk that stopped early must be distinguishable from one that finished.** Nine anime because that is all there were, and nine because the budget ran out, must not look identical to a caller. The result therefore reports which bound ended the walk, if any — the budget, the depth, or an id AniDB does not recognise — and reports nothing when the walk simply ran out of graph.

This is not a convenience. An answer shaped like a complete one that is not is the failure this library has already shipped twice, and it is worse than an error: the caller acts on it, gets a plausible result, and has no reason to doubt it. Silent truncation is the same class of defect as a wrong shape returned under the right name.

A depth limit that is actually reached is reported as a bound whether or not anything lay beyond it. The library does not check: finding out means reading the relations of every anime on the boundary, which costs exactly the requests the bound was set to prevent.

When more than one bound applies, the one that ended the walk is the one reported.

## Why no type filter is correct, and no default can be

`other` conflates two genuinely different things, and does so on real records rather than in principle. Read from the live service, *Gundam: Iron-Blooded Orphans* relates by `other` to the 1979 *Kidou Senshi Gundam* — the franchise ancestor, a different show by any reading — and, by the same `other`, to its own special edition and its own side story. Two of the three are the same show and one is not, and nothing in the type distinguishes them.

So a filter that excludes `other` loses real content, and one that includes it inherits the entire Universal Century. **No type filter is correct**, which is precisely the argument for returning the type to the caller rather than choosing on their behalf. Disambiguating "the 1979 series under a different name" from "this show's own special edition" is judgment applied to evidence — titles, dates, the caller's own knowledge of what it is reconciling — and it belongs where that evidence is.

This is also why the default walk is bounded mechanically rather than semantically. A story-relations default (`sequel`, `prequel`, `side story`, `parent story`) would look principled and would be quietly wrong on exactly the record above, dropping two entries out of three that belong. The budget encodes no opinion at all: it says only how much work a walk may do.

## What AniDB's graph actually looks like

Measured against the live service, and the reason the bound above is a default rather than an option.

**Seasons are not linked to seasons.** The chain threads through whatever OVAs and movies shipped between them:

```txt
8265  Maken-ki!            (TV)
  -> 8566   Maken-ki! OVA
  -> 10191  Maken-ki! Two OVA
  -> 9406   Maken-ki! Two  (TV)
```

Any answer to "the seasons of this show" is therefore transitive, and a fixture or an example built on a single relation demonstrates nothing about it.

**Sprawl arrives through `other`, and it is unbounded in practice.** From *Iron-Blooded Orphans*, one `other` edge reaches the 1979 original, and through it decades of Universal Century series, each linked onward. The size of that component was deliberately not measured — walking it to find out is the ban this bound exists to avoid.

**The story types carry genuine spin-offs correctly.** A caller who names them gets a useful, tight answer:

```txt
5975  Toaru Majutsu no Index
  sequel       -> 7599   Index II
  side story   -> 6460   Toaru Kagaku no Railgun
  side story   -> 14440  Toaru Kagaku no Accelerator
6460  Toaru Kagaku no Railgun
  sequel       -> 9484   Railgun S
  parent story -> 5975   Index
```

A release packaging *Index* together with *Railgun* is reachable without opening the walk up to everything.

## Related Artifacts

- **Line of truth (self-enforcing):** `src/anidb_client/db.py` — the relation rows and the enumeration of relation types the wire layer selects from. The AniDB UDP API is the external contract the types are transcribed from.
- **Related specs:** SPEC-001 (the object layer these reads happen through, and what a read that cannot be answered does); SPEC-003 (how relation rows are cached and reconciled, and when a cached row counts as fresh); SPEC-002 (the pacing and ban back-off that make a walk's request count matter).
- **Related decisions:** ADR-005 (why the library returns the graph and the caller decides what a show is).
- **Tests:** `tests/unit/test_relation_walk.py` covers the walk end to end — the multi-hop chain, resolution of anime the cache has never seen mid-walk, each bound, and the reporting of which bound ended a walk. Relations whose type or related id cannot be read are covered in `tests/unit/test_enum_converters.py`.
