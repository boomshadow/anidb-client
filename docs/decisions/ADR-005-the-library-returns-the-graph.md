---
title: "The Library Returns the Graph; the Caller Decides What a Show Is"
description: "AniDB's relation graph is the authoritative answer to what belongs to a show, so anidb-client returns it whole — relation types included as data — and bounds a traversal only mechanically, by a budget and an optional depth, rather than semantically by relation type. Filtering by type inside the library was rejected because it discards evidence the caller cannot recover; a story-relations default was rejected because the measured `other` relation carries same-show entries two times in three, so no type filter is correct for everyone; mylist membership as the walk's bound was rejected because it is a use-case filter that returns nothing at all for callers who do not use mylist; discarding the relation types was rejected as less faithful to AniDB than reporting them; and silent truncation was rejected because an incomplete answer shaped like a complete one is the failure mode that produced this decision."
status: accepted
tags: [relations, relation-graph, related-anime, authority, division-of-labour, traversal, budget, truncation, relation-types, other, pre-filtering, rate-limiting, ban-avoidance, api-design, breaking-change]
---

# The Library Returns the Graph; the Caller Decides What a Show Is

## Context

The question "what else belongs to this show" has no local answer. A show's seasons, its OVAs, its side stories and its franchise siblings are not derivable from a title, a directory name or a file's metadata — the relationship between two anime is a fact about the works, held externally.

AniDB holds it. Its relation graph is the anime community's collective judgment, curated over decades, and it is the closest thing to a machine-readable answer that exists. **It is the authority**, and the library's reason for existing here is to make that authority easy to reach.

Nothing else on the path has authority of its own. An application calling this library does not know how two anime relate except by asking; if it is an AI agent, it can produce a season number with complete confidence and no basis at all. A human operator is a better judge and still not a database — one person's recollection of a franchise is a recollection.

That is not hypothetical. The incident behind this decision had a downstream application resolve a season-two mapping "from my own research rather than AniDB's official chain", because the tool that should have answered came back empty. It happened to be right. The design goal here is to remove the conditions that made guessing look reasonable.

Two properties of the real graph shape what "easy to reach" has to mean.

**It is transitive.** AniDB does not link a season to its next season; the chain threads through whatever OVAs and movies shipped between them. `Maken-ki!` reaches `Maken-ki! Two` through two OVAs. So any useful answer requires a walk, not a single read.

**It is unbounded in one direction.** From *Gundam: Iron-Blooded Orphans*, an `other` edge reaches the 1979 original, and through it decades of Universal Century series, each linked onward. Against an API that bans clients for asking too often, an unbounded walk on a franchise like that is not slow — it is a ban.

And the vocabulary that would be the obvious way to bound it does not divide cleanly. On the *Iron-Blooded Orphans* record, `other` carries both the 1979 ancestor and two entries belonging to *Iron-Blooded Orphans* itself.

## Decision

**The library returns the graph. The caller decides what "the same show" means.**

Concretely:

- **`related_anime()` returns the relation type with every anime it reached**, in the same `(relation_type, Anime)` shape `relations` already uses. The type is discovered during the walk and cannot be recovered afterwards without walking again, so discarding it destroys information at the only moment it is free.
- **The library applies no relation-type filter of its own, at any time, including by default.** A caller who names types to follow gets them applied; a caller who names none gets every edge followed.
- **A walk is bounded mechanically**: a traversal budget capping how many anime it may reach, on by default, and an optional depth. Both cap work rather than deciding relevance.
- **A walk that stopped early says so**, naming the bound that ended it.
- **The relation types are data, not policy.** The library reads them, returns them, and applies none of them.

The last point is the one that took longest to reach, and both halves of it matter. The types are AniDB's own encoding of how two entries relate, so *reading* them is deferring to the database — and flattening them away, answering an undifferentiated set of anime, would discard a distinction the community deliberately drew and would be less faithful to AniDB than reporting it. Deference means returning them, not applying them.

The division of labour that follows is: **the library makes it easy to get the list; the caller reconciles that list against reality** — which files are actually in this release, which are junk, which are misnumbered, where the OVAs go. That reconciliation needs evidence the library does not have and cannot get.

Five alternatives were considered and rejected.

**Filter by relation type inside the library.** Rejected because it decides relevance before anyone has looked at the evidence. If a release contains an entry AniDB links by a type the filter excluded, the record needed to identify that file was discarded before the caller ever saw it, and cannot be recovered without re-walking the graph — which is the work the call exists to do. Pre-filtering can only lose information it has not earned the right to discard; returning everything and reconciling downstream cannot.

**Default to story relations** — `sequel`, `prequel`, `side story`, `parent story` — as a sensible bound that keeps a walk on one show. Rejected on the measurement. On the *Iron-Blooded Orphans* record, `other` carries the franchise ancestor *and* the show's own special edition *and* its own side story: two of the three are the same show. A story-relations default is therefore quietly wrong two times in three for that type, and quietly is the operative word — the caller sees a plausible, tidy answer with content missing and no indication that anything was dropped. It also puts the library in the business of deciding what a show is, which is the line this decision draws. The alternative is not a better filter; it is that **no type filter is correct for everyone**, and the ambiguity should be surfaced rather than papered over.

**Keep mylist membership as the walk's bound.** This is what the function did, and the reasoning recorded for it — that otherwise one sequel link drags in an entire franchise — diagnosed the right problem on the wrong axis. Rejected because it is a use-case filter wearing a safety filter's clothes: it works for someone cataloguing a collection they already have, and for a caller who does not use mylist it rejects every neighbour, so the walk returns only the anime it started from. That is not a bounded answer, it is an empty one, and it was the default. Once a budget does the safety job, mylist membership has no second role and is off unless asked for.

**Return just the anime and let the caller re-derive the types.** Rejected because they cannot. The types exist in the walk and nowhere in its result; recovering them means walking the graph again, over a rate-limited API, to learn something the first walk already knew.

**Truncate silently when the budget runs out.** Rejected outright, and this is the strongest constraint in the decision. An answer shaped like a complete one that is not is the exact failure mode that produced this work — twice, once as a wrong-shaped value returned under the right name and once as a tool that came back empty and invited a guess. "Nine because that is all there are" and "nine because you hit the ceiling" must be distinguishable, or the caller cannot know whether to trust what they have. A bounded answer that announces its bound is honest; one that does not is worse than an error, because an error stops the caller and this does not.

## Consequences

**The return shape changes and existing callers break.** `related_anime()` answered a `list[Anime]` and now answers a result object carrying pairs and the bound that ended the walk. The `only_in_mylist` default flips, which changes what an existing call returns without raising. Both are breaking, deliberately: the API is young enough that fixing it properly costs less than carrying a lossy one and a duplicate beside it, and a version number is cheap.

**Callers that want one show's timeline do slightly more work, and get a correct answer.** They name the relation types they trust and reconcile the rest themselves — typically one walk over story relations for the reliable core, plus a look at the root's own `other` links, judged by title and date. That is one extra read, and it is precisely the reconciliation this decision places downstream.

**The default walk still costs requests.** A budget bounds them, it does not remove them; a caller walking many anime should expect the pacing in SPEC-002 to be what governs their runtime.

**The library will not gain a "the same show" helper later without revisiting this ADR.** Any such helper is a semantic judgment, and the argument above is that the library does not have what it takes to make one. A convenience naming a set of relation types is the same judgment in a smaller package, and is deliberately not shipped.
