---
title: "The Releases Page Is the Changelog"
description: "This project keeps no CHANGELOG.md. The release pages on GitHub and GitLab are the changelog, carrying a generated section listing every non-merge commit grouped into Keep a Changelog's headings, under a short written paragraph saying what the release is about. A hand-curated CHANGELOG.md was rejected because the practice's value is readability rather than the file, and the file demands discipline at every merge; a CHANGELOG.md generated and committed by CI at tag time was rejected because it needs a credential that can push to a protected branch and leaves the changelog inside every published artifact one release stale; pure generation with no written summary was rejected because a list of commit subjects does not say what a release was for."
status: accepted
tags: [changelog, release-notes, keep-a-changelog, git-cliff, conventional-commits, releases, github, gitlab, mirror, documentation, generation, curation]
---

# The Releases Page Is the Changelog

## Context

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) makes an argument worth taking seriously: a changelog is for humans, and a dump of commit subjects is not a changelog. Its prescription is a hand-maintained `CHANGELOG.md` with an `[Unreleased]` section that authors append to as they work.

The argument and the prescription are separable. The argument is about **readability**. The prescription is one way to get it, from an era when the alternative was a raw `git log`.

Two properties of this project bear on the choice. Its commit subjects are full sentences written to be read — the squash-merge workflow means `main` carries one well-formed subject per merge request rather than a stream of work-in-progress commits. And its public face is a mirrored repository whose releases page is where a reader actually lands, because the source of truth for the project is a private GitLab project that most readers cannot open.

## Decision

**There is no `CHANGELOG.md`.** The release pages on GitHub and GitLab are the changelog, and both carry the same body.

That body has two halves. The lower half is **generated** from the commits in the release: every non-merge commit, grouped into Keep a Changelog's headings, each linking to the public mirror. Nothing is filtered out — an internal or infrastructural change is still something a reader may be trying to locate months later, and grouping is what keeps the list readable rather than omission. The upper half is a short **written** paragraph saying what the release is about, which is the part a generator cannot produce.

`cliff.toml` is the line of truth for the generated half. SPEC-009 describes the behavior.

Three alternatives were considered and rejected.

**A hand-curated `CHANGELOG.md`, per Keep a Changelog's prescription.** Rejected on both halves of the trade. The benefit is readability, and the written paragraph delivers that without the file. The cost is real and recurring: an `[Unreleased]` section must be maintained at every merge request, which is a discipline that decays exactly when the project is quiet, and it conflicts whenever two branches are open at once. It also fails the consumption test — the reader this project actually has goes to a releases page, not to a file in a source tree.

**A `CHANGELOG.md` generated and committed by CI when a tag is pushed.** Rejected for two independent reasons. It requires a credential in CI that can push to a protected branch, which is a long-lived secret introduced to solve a problem that has a cheaper answer. And the commit necessarily lands *after* the tag, so the `CHANGELOG.md` inside every published artifact is permanently one release out of date — a document that is wrong in the same way every single time.

**Pure generation, with no written summary.** Rejected because it concedes Keep a Changelog's actual point. A list of commit subjects records what changed without saying what the release was *for*, and the reader has to reconstruct the theme from the parts. The generated list is a good index and a poor summary; the paragraph above it is the summary.

## Consequences

Nothing in the published wheel or sdist describes what changed between versions. A reader looking for that is sent to the releases page, which is where the README already points.

**Commit subjects are user-facing copy.** This is the significant consequence and it reaches back into everyday work. A merge request title becomes the squashed commit's subject, and that subject becomes a line in a release note that users read. Two things follow: a title has to describe the whole merge request rather than the change it started as, and the Conventional Commits *type* is a routing decision rather than decoration, because it selects the heading the change appears under. A merge request that bundles two notable changes permanently collapses them into one line.

That coupling is enforced where it can be. The grammar of a merge-request title and of every subject reaching the default branch is checked by CI, and the accepted type vocabulary is held against the routing table by a test, so a type nothing routes cannot quietly fall into the catch-all.

What remains is the part no gate can reach: whether a well-formed title is *accurate*. A title describing only what a change started as passes everything and still misfiles the entry. So the decision does cost discipline at every merge — the same currency the hand-curated changelog was rejected for demanding. The differences are that it is owed once per merge request rather than once per change, that the generated list stays complete whether or not the discipline holds, and that the mechanical half of it is enforced rather than remembered.

Release notes are not reproducible. The generated half is deterministic; the paragraph above it is written once and is not derived from anything. That is the point, and it means a release note is authored rather than computed.

Commits that predate the Conventional Commits discipline land in a catch-all group. They are shown rather than hidden, because a commit being unparsable is not a reason for the work to vanish from the record.

## Related Artifacts

- **Specs:** SPEC-009 describes release and publication behavior, including what a release note contains and how it is produced.
- **Decisions:** ADR-001 records why the README stays a complete user-facing document — it is the reason a reader arriving from PyPI has somewhere to be sent.
- **Line of truth:** `cliff.toml` (the grouping, the commit filter, and the release boundary); `docker-compose.yml` (the pinned generator).
