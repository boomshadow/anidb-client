---
title: "Releases Are Orchestrated Locally, CI Owns Only the Publish Gate"
description: "Cutting a release runs on the maintainer's machine through an agent skill, using already-authenticated glab and gh, while CI keeps exactly one job: verifying the tag and uploading to PyPI over OIDC trusted publishing. Driving the whole release from CI was rejected because GitHub accepts no third-party OIDC, so creating the GitHub release would require the only long-lived stored credential in the system — a repo-scoped token with a yearly rotation chore — to save one command; adding GitHub Actions for the GitHub half was rejected as a second CI system and supply chain for one API call; a GitLab-only release was rejected because the GitLab project is private and GitHub is the public face; and a purely manual checklist was rejected because months pass between releases and the steps are exactly what gets forgotten."
status: accepted
tags: [release, publishing, ci, orchestration, credentials, oidc, trusted-publishing, github, gitlab, personal-access-token, skill, agent, automation, supply-chain, mirror]
---

# Releases Are Orchestrated Locally, CI Owns Only the Publish Gate

## Context

Cutting a release of this project means doing six things: choosing a version, writing release notes, tagging, uploading to PyPI, and creating a release page on each of GitLab and GitHub.

Five of those are mechanical. One is not — a release note is only worth reading if something says what the release is *about*, which is a judgement about a body of work rather than a transformation of it (ADR-003).

The obvious arrangement is to put all six in CI, triggered by pushing a tag. Three facts make that less obvious than it looks.

**PyPI accepts an identity; GitHub accepts a secret.** PyPI's trusted publishing verifies a short-lived OIDC token GitLab issues at job time, so there is no credential to store. GitHub has no equivalent for a third party: nothing GitLab can present will authenticate an API call to GitHub. Creating a GitHub release from GitLab CI requires a stored, long-lived token.

**The GitLab project is private and GitHub is the public face.** `main` is mirrored to a public GitHub repository, and that is where a reader arriving from PyPI's Source link lands. A release visible only on GitLab is a release most readers cannot see.

**Releases here are infrequent.** This is a library with one maintainer. Months can pass between releases, which is long enough to forget the tag spelling, the order of operations, and which of them cannot be undone.

## Decision

**Release orchestration runs locally**, driven by an agent skill (`/publish`) using the maintainer's already-authenticated `glab` and `gh`. It performs the preflight checks, decides the version, generates the notes and writes the paragraph above them, tags, waits for CI, and creates both release pages.

**CI keeps exactly one responsibility**: verifying that the tag is legal and that the built artifacts match it, then uploading to PyPI over OIDC. SPEC-009 describes that gate.

The division is that **taste is local and gates are in CI**. Judgement — what version this is, what the release is about, whether the notes read well — happens where judgement lives. The checks that must never be skipped happen where they cannot be.

Four alternatives were considered and rejected.

**Drive the whole release from CI, with a stored GitHub token.** The conventional arrangement, and rejected on what it costs. It introduces the *only* long-lived credential in the system: a token that can write to the public repository, sitting in CI variables, carrying a yearly rotation chore and a standing question about who can read it. Everything else here is either short-lived or has no credential at all, and this would spend that property to save the maintainer one command. It also cannot write the paragraph, so the notes would either lose their written half or need a model invocation wired into the pipeline — more moving parts, another credential, in service of the same command.

**Add GitHub Actions for the GitHub half.** Rejected. CI runs on GitLab deliberately; GitHub holds a mirror and runs nothing. Introducing a second CI system — with its own supply chain, its own permissions model, and its own failure modes — to make one API call is disproportionate to the call.

**Publish a GitLab release only, and skip GitHub.** Rejected because it optimises for the wrong reader. The GitLab project is private; the audience for a release note is the audience that installs the package, and they land on GitHub. This would put the changelog where the people who need it cannot reach it.

**A manual checklist in the documentation, with no skill.** Rejected on the infrequency. A checklist followed once every few months is a checklist half-remembered, and the failure modes here are unusually unforgiving — a published version can never be reused and a pushed tag can never be moved. A skill is discoverable, states its own preflight conditions, and refuses to continue when one fails. It is also self-enforcing in the sense SPEC-000 means: the agent reads it and executes it, so it cannot quietly drift from what actually happens the way a prose runbook does.

## Consequences

**No long-lived credential exists anywhere in the release path.** The only credential involved is the short-lived token PyPI mints for itself during upload. This is the property the decision exists to protect, and any future change that reintroduces a stored token should be weighed against it.

**A release depends on the maintainer's machine.** The right branch, a clean tree, current refs, and two authenticated CLIs. The skill checks all four before doing anything, and stops rather than repairing, but the dependency is real: a release cannot be cut from anywhere else.

**Release notes are authored rather than computed.** Re-running produces different prose. That follows from ADR-003 and is intended, but it means a release note cannot be regenerated to match one already published.

**One commit is pushed directly to `main` per release** — the version bump, which must exist and be tagged for the publish gate to pass. This is a deliberate carve-out from the rule that all changes arrive by merge request, and the narrowest one available: it is a single mechanical edit made by tooling, and its subject is still checked by the same conventional-commit gate as everything else on `main`.

**The GitHub tag arrives asynchronously.** The mirror pushes it rather than the release process creating it, so the GitHub release is created against a tag that must already have landed. In practice the CI pipeline takes longer than the mirror; the skill verifies rather than assumes, because letting the release create its own tag would leave GitHub with a lightweight tag where GitLab has an annotated one.

## Related Artifacts

- **Specs:** SPEC-009 describes the release behavior this decision arranges — the tag grammar, the gate, and what a release note contains. SPEC-008 describes the pipeline whose single publish job is the CI half.
- **Decisions:** ADR-003 explains why release notes are half generated and half written, which is the reason the process needs judgement in it at all. ADR-002 covers the version notation the skill has to produce.
- **Line of truth:** `.claude/skills/publish/SKILL.md` (the procedure itself, read and executed by the agent); `.gitlab-ci.yml` (the publish job it waits on).
