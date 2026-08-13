---
title: "SemVer in the Repository, PEP 440 in the Artifact"
description: "Release tags and the declared package version are written in SemVer, and the build backend normalises to PEP 440 when it builds the distribution, so only one version notation is ever written by hand — with the accepted tag grammar narrowed to the alpha/beta/rc pre-releases PEP 440 can express. Tagging in PEP 440 was rejected because a git tag is read outside Python and should not speak a packaging dialect; deriving the version from the tag with setuptools_scm was rejected because it needs a git binary the pinned toolchain image deliberately lacks and a .git directory the build context deliberately excludes, and it would discard the check that catches a tag on the wrong commit."
status: accepted
tags: [versioning, semver, pep440, tags, release, packaging, hatchling, setuptools-scm, hatch-vcs, normalisation, pre-release, build-metadata, supply-chain, docker]
---

# SemVer in the Repository, PEP 440 in the Artifact

## Context

Python packaging mandates PEP 440. It is not optional: a distribution whose version is not a PEP 440 version cannot be built, uploaded or installed.

SemVer is the notation this project uses everywhere else, and it is what a git tag is normally expected to carry. The two schemes agree exactly on ordinary releases — `0.1.0` means the same thing in both — and disagree only on pre-releases. SemVer separates the pre-release with a hyphen and a dot, `0.1.0-rc.1`; PEP 440's canonical form has neither, `0.1.0rc1`.

That disagreement is narrower than it first appears. PEP 440's grammar admits `.`, `-` and `_` as optional separators on both sides of the pre-release identifier and normalises them away, so the SemVer spelling is a **valid non-canonical PEP 440 version** rather than an invalid one. A build backend handed `0.1.0-rc.1` produces a distribution labelled `0.1.0rc1` without complaint.

So the question is not how to translate between two incompatible schemes. It is which notation the repository should speak, given that the artifact must speak PEP 440 regardless.

Two further facts constrain the answer, both properties of this project rather than of Python:

- The pinned toolchain image carries **no git binary** — a deliberate choice, and the reason the spec and ADR index checks are performed inside their generator rather than by diffing the working tree.
- `.git/` is excluded from the Docker build context, so the development image is built without a repository present at all.

## Decision

**The repository speaks SemVer and nothing else.** The declared package version is written in SemVer, and the release tag is exactly `v` followed by that string. No PEP 440 spelling appears anywhere in the repository.

**The build backend performs the normalisation.** The wheel and sdist carry the PEP 440 form. That notation exists only in artifact filenames and on PyPI — never in a file anyone edits.

**The accepted tag grammar is narrowed to what PEP 440 can express**: three numeric components, optionally followed by an `alpha`, `beta` or `rc` pre-release and a number. Build metadata and arbitrary pre-release identifiers are refused. A single gate enforces this and is applied on both sides of the tag — before one is created, and again before anything is uploaded. SPEC-009 describes that behavior.

Three alternatives were considered and rejected.

**Tag in PEP 440 — `v0.0.1rc1`.** The simplest option: the tag, the declared version and the artifact would all be one string, and no translation would exist anywhere. Rejected because it puts the packaging ecosystem's dialect into the wrong artifact. A git tag is read by things that have no idea Python is involved — the public mirror, release pages, anyone pinning a ref — and PEP 440's compact pre-release spelling is a Python convention, not a general one. Adopting it would make the git history speak a language chosen by the build tool, inverting which notation is primary for a project that has settled on SemVer. The cost avoided is small and bounded: one translation, in one place, with tests.

**Derive the version from the tag, with `hatch-vcs` or an equivalent.** Genuinely attractive — the tag would be the only place a version exists, nothing in the repository would spell one at all, and the declared version could never fall out of step with the tag because there would be nothing to fall out of step. Rejected on the two constraints above. Every such plugin reaches `setuptools_scm`, which shells out to the git binary; serving it would mean adding an unpinned apt package to the supply chain and un-ignoring `.git/` in the build context, inverting two settled decisions in order to remove one string from one file. Feeding the version in through an environment variable instead, so no repository access is needed, was rejected in turn: it keeps two build dependencies while reducing them to reading an environment variable, it makes local builds report a placeholder version, and it makes the tag-versus-artifact comparison tautological — the tag becomes the input, so the check can no longer fail, discarding the one guard that catches a tag sitting on the wrong commit.

**Accept the full SemVer pre-release grammar and translate it.** Rejected because no total mapping exists. SemVer permits arbitrary dot-separated identifiers and a `+` build-metadata segment; PEP 440 has no equivalent for the first, and PyPI refuses uploads carrying the second in any form. A translator would therefore have to either invent a version the author did not choose or fail — and failing is what the narrowed grammar already does, except that it fails at the tag, before anything is published, and names the spelling that would have worked.

## Consequences

Two notations exist, but only one is ever typed. The PEP 440 form appears in filenames and on PyPI and nowhere a person edits, so the cost of the split is paid entirely by machines.

The mapping between them is load-bearing code with its own tests rather than an assumption in a comment. It decides what reaches PyPI, so it is type-checked and covered to the same standard as the library — which is why the scripts directory is inside the type checker's scope rather than treated as tooling.

**The reported version and the installed metadata differ in spelling for a pre-release.** A caller reading the package's declared version sees `0.0.1-rc.1` while the installed distribution metadata says `0.0.1rc1`. Comparing the two literally will surprise someone. The test suite pins the relationship by comparing them through the same translation the publish gate applies, which also verifies that the build backend normalises the way the gate predicts rather than assuming it.

The declared version must be bumped as part of cutting a release, because it is not derived from anything. A forgotten bump is caught by the gate, which compares the tag against the version the built artifacts actually carry — the guard that the rejected tag-derived approach would have removed.

The narrowed grammar means a legitimate SemVer tag can be refused. That is intended: the refusal happens before the tag exists, where it costs nothing, rather than at upload time or — worse — not at all.

## Related Artifacts

- **Specs:** SPEC-009 describes the release and publication behavior this decision underpins, including the tag grammar and the gate. SPEC-006 covers the separate AniDB client identity, which is a registration number and moves independently of this version.
- **Line of truth:** `scripts/release_tag.py` (the grammar and the mapping); `pyproject.toml` (the build backend and where it reads the version from).
