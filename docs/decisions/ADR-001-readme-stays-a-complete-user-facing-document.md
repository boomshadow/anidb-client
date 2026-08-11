---
title: "README Stays a Complete User-Facing Document"
description: "Adopting Anchored Development would normally reduce README.md to a navigation aid and move its behavioral prose into specs, but this README is also the package's PyPI long description, read by people evaluating the package rather than working on it, so it keeps its full reference content and accepts deliberate overlap with the specs — which hold authority when the two disagree. Publishing a separate document to PyPI and gutting the root README was rejected because relocating user-facing prose does not change its enforcement mode; reducing the README to pointers was rejected because it degrades the PyPI page; generating it from the specs was rejected as disproportionate."
status: accepted
tags: [readme, navigation-aid, pypi, long-description, packaging, anchored-development, deviation, self-proving, documentation, duplication, drift]
---

# README Stays a Complete User-Facing Document

## Context

Anchored Development classifies a README as a **navigation aid**: a document whose job is to route readers to artifacts, not to contain authoritative information itself. The framework's guidance for adoption is to extract behavioral information from existing documents into specs and leave the original as a router.

This project's `README.md` does not fit that classification cleanly, because it serves two audiences at once. It is declared as the package's `readme` in `pyproject.toml`, which makes it the **long description rendered on the PyPI project page**. So besides the reader standing in the repository, it also serves someone evaluating or installing the package from PyPI — who has no `docs/specs/` directory in front of them and, in the general case, has not visited the repository at all.

That second role is a **choice, not a constraint**. The `readme` field accepts any relative path, so a project may publish some other file to PyPI and leave its root `README.md` to be whatever it likes. Which file plays which role is therefore part of this decision rather than a fact it has to work around.

The README currently carries genuine reference content: the object API surface, `init()`'s arguments, the netrc machine-name rules, the caching and rate-limit summaries, the fanart preconditions, and the upgrade warning that the cache has no migration path. Under a literal reading of the framework, most of that is spec material.

The framework anticipates exactly this situation and provides for it: a project that needs to deviate documents the deviation using the framework's own mechanisms. That is what this ADR is.

## Decision

The repository's root `README.md` **stays the file published to PyPI**, and remains a **complete, self-contained user-facing document**. It keeps its installation guide, usage examples, API reference and operational notes, and it is not reduced to a set of pointers into `docs/specs/`.

It gains one addition: a short section naming Anchored Development and pointing at the spec and ADR directories, so a reader who *is* in the repository is routed onward. That section routes; it does not replace what is already there.

The overlap this creates between the README and the specs is **accepted and deliberate**, with a clear division of authority:

- **The specs are authoritative** for behavior. When the README and a spec disagree, the spec is correct and the README is the thing to fix.
- **The README is authoritative** for nothing — it is still a navigation aid in the framework's sense. What changes is only how much it may say before pointing onward.
- The drift detector is told that README/spec overlap is intended, and that drift is the two *disagreeing*, not the two both describing the same behavior.

Three alternatives were considered and rejected.

**Publish a separate document to PyPI and gut the root `README.md`.** Point `readme` at, say, `docs/pypi.md`, let that file carry the full user-facing content, and reduce the root README to the navigation aid the framework prefers. Rejected because it relocates the concern without addressing it. The framework's objection is to unverified prose that can drift from the code, and a complete user-facing document duplicates the specs exactly as much at `docs/pypi.md` as at `README.md` — its enforcement mode is identical either way, so this ADR would still be needed, naming a different file. Against that nil benefit it costs two user-facing documents to keep in step with the specs and with each other instead of one, and it makes the file every code host renders first the least informative document in the repository. Worth revisiting only if the two audiences' needs genuinely diverge — if the PyPI page came to want shaping that would be wrong for a repository README.

**Reduce the README to a navigation aid and move its reference content into specs.** This is the framework's default and it was rejected on its effect at the point of consumption. The PyPI page would become a stub linking to a repository, which is a materially worse experience for the audience the file exists to serve — evaluating a library means reading what it does, and "see the linked repository" is a poor answer. The framework's concern is documentation that has no consumer and no enforcement mechanism; this README has a large consumer and, through the specs and the drift gate, an enforcement mechanism. Applying the letter of the rule here would damage the thing without addressing the risk.

**Generate the README from the specs, or transclude spec sections into it.** Rejected as disproportionate. It requires build machinery and a generation step in the release path, and the two documents genuinely want different shapes: the README is organised as a tutorial and reference for a new user, the specs by behavioral domain. Concatenating domain specs would not produce a good README, and shaping specs to read well when concatenated would compromise them as specs.

## Consequences

The same behavior is described in two places, so a behavioral change has two documents to update rather than one. This is the real cost, and it is the cost that Anchored Development exists to warn about. It is bounded by the specs holding authority — a conflict has a defined winner, so reconciling one is a mechanical edit rather than an investigation — and by the drift gate seeing both files.

The drift detector needs to know about this decision or it will report the overlap itself as a finding on every change that touches both. That instruction lives in the detector's own rules, which makes this ADR load-bearing rather than merely explanatory.

The PyPI page stays a complete description of the package, and adopting Anchored Development is invisible to users installing from PyPI.

New behavioral documentation defaults to the specs. The README grows only when something a *user* needs at install or evaluation time changes; it is not the place where new behavior gets written down first.
