---
title: "Release and Publication"
description: "How a version of this library reaches its users: choosing the number by SemVer against the public surface, and the freedom to change one nobody can install yet; the version declared in the package as SemVer and normalised to PEP 440 in the built artifact; the release tag grammar, which admits only alpha/beta/rc pre-releases and refuses build metadata; the permanence of tags and published versions and the fix-forward rule that follows from it; the gate that rejects a malformed tag loudly rather than publishing nothing silently; release notes carried on the release pages rather than in a changelog file, half generated from the commits and half written; the conventional-commit grammar checked on both sides of the squash — merge-request titles and the subjects reaching the default branch — with the accepted type vocabulary held against the routing table in both directions so a type nothing routes cannot fall silently into the catch-all; and upload to PyPI over OIDC trusted publishing with no stored credential."
status: accepted
tags: [release, publication, versioning, semver, pep440, tags, protected-tags, pre-release, release-candidate, immutability, fix-forward, gate, pypi, trusted-publishing, oidc, publish, release-notes, changelog, keep-a-changelog, git-cliff, conventional-commits, mirror]
---

# Release and Publication

A release is a version of this library made available to people who are not working on it. This spec covers how a version is chosen, how it is written down and marked, what refuses to publish, and what can never be taken back.

## Choosing a version

The number is chosen by SemVer's rules, read against the library's public surface — the objects, their attributes, `init()`'s arguments and the exceptions raised. A **patch** release changes behavior a caller did not depend on; a **minor** release adds something a caller can newly rely on; a **major** release breaks something a caller already relied on. A leading zero major is the standing signal that the surface is not yet stable and any release may break it.

A **pre-release** answers a different question: not how much changed, but whether it has been proven. It is the correct choice when a change needs to run against a real consuming application before its number is committed to ordinary users.

**A version that has never been published may be changed freely, including downwards.** Nothing outside the repository depends on a number no one can install. That freedom ends the instant a version is published — from then on the rules below apply and the only direction is forward.

## Where the version lives

The distribution's version is declared once, in the package itself, and the build backend reads it from there. It is written in **SemVer** — the same notation the release tag uses — so the tag for any release is exactly `v` followed by that string, and the two can be compared without translating either.

The built artifact carries the **PEP 440** spelling instead, because that is the only notation Python packaging accepts. For an ordinary release the two are identical. For a pre-release they differ: SemVer writes `0.1.0-rc.1` and PEP 440 writes `0.1.0rc1`. The build backend performs that normalisation; nothing in the repository states the PEP 440 form. ADR-002 records why the repository speaks only SemVer.

The version is deliberately unrelated to the client identity registered with AniDB, which is a registration number rather than a release number. SPEC-006 covers that distinction.

## What a release tag may look like

A release tag is `v` followed by a SemVer version: three numeric components, optionally followed by a pre-release of `alpha`, `beta` or `rc` and a number — `v0.0.1`, `v1.2.3`, `v0.0.1-rc.1`.

Two things SemVer permits are refused:

- **Build metadata**, the `+` segment. PyPI rejects every version carrying a local label, so a tag with one could never publish however well-formed it looks. It is refused with its own explanation rather than the generic one, because the tag is valid SemVer and a generic refusal would read as a defect in the gate.
- **Any other pre-release identifier.** SemVer allows arbitrary dot-separated identifiers; PEP 440 can express only the three above. A tag spelling Python cannot represent would otherwise publish under a version nobody chose.

A refusal always names the spelling that would have been accepted. The gate fires months apart, at the exact moment the grammar has been forgotten, so telling the reader what to type is the whole point of the message.

## Tags and published versions are permanent

**A tag that has published something never moves.** This is not a convention; three separate mechanisms make it true.

- PyPI never allows a version to be re-uploaded, even after it is deleted. A version number is spent the moment it is published.
- Release tags are protected, so GitLab refuses to update one at all and permits deletion only through its own interface — a local delete-and-repush is rejected outright.
- The repository is mirrored publicly, so a tag that has been observed elsewhere cannot be un-observed.

The consequence is the **fix-forward rule**: when a tagged build fails, or a published release turns out to be wrong, the answer is the next version, never a correction to the existing one. Skipped version numbers are ordinary and carry no meaning.

## The gate

The same check runs on both sides of a tag: before one is created, and again in the pipeline before anything is uploaded. It answers two questions.

**Is this a legal release tag?** If not, the pipeline fails and names the correct spelling.

**Do the built artifacts carry the version the tag implies?** Every wheel and sdist is read by filename, so what is checked is what is about to be uploaded rather than the source it was built from. A disagreement means either the tag sits on the wrong commit or the declared version was never bumped to match it. A build directory containing no distribution at all is also a failure — a publish that uploads nothing must not report success.

**A malformed tag fails loudly.** Every `v`-prefixed tag reaches the publish job, and legality is decided there. Filtering malformed tags out earlier would mean running no job at all, which is indistinguishable from a successful publish to whoever pushed the tag — the failure most likely to go unnoticed, and the one this arrangement exists to prevent.

## Publishing

Upload happens only from a tag, and only after every other stage has passed. It uses **OIDC trusted publishing**: PyPI verifies a short-lived identity token issued by GitLab rather than a stored API token, so there is no upload credential in the pipeline at all.

PyPI's side of that trust is configured against four things — the namespace, the project name, the path of the top-level pipeline file, and the deployment environment the job declares. The job's *name* is not among them, so renaming the job is safe while moving the environment declaration to a different job is not.

## Release notes

There is no changelog file. The release pages carry the changelog, and both the GitLab and the GitHub release get the same body. ADR-003 records why.

A release note has two halves.

The **generated** half lists every commit in the release, grouped into Keep a Changelog's headings — Added, Changed, Fixed, Removed, Deprecated, Security — with documentation and internal work under headings of their own below those. Each entry links to the commit on the public mirror, because the same body is posted to a release page most readers reach without access to the private project.

Nothing is filtered out except merge commits, which carry no information their squashed commit does not already carry. An infrastructural or test-only change is still something a reader may be trying to locate months later; grouping is what keeps the list readable, not omission. A commit that is not a well-formed conventional commit still appears, under a catch-all heading.

The **written** half is a short paragraph above the list saying what the release is about. A generator cannot produce it, and it is the half that answers the question a reader actually arrives with. It is authored, so it is not reproducible — unlike the list below it, which is.

**Where a release begins** is the previous tag. The tag marking the state this project inherited before its own work began counts for that purpose, which is what keeps the first release's notes scoped to this project's changes rather than to the entire history of the code it was forked from.

Because a merge request is squashed on merge, its title becomes one line of a release note. That makes the title user-facing copy and the conventional-commit type a routing decision — the type selects the heading the change appears under.

The grammar is checked, on both sides of the squash. A merge request's title is validated on every merge-request pipeline, because that title is what becomes the subject. The subjects that land on the default branch are validated again on push, which catches the two routes that bypass the first check entirely: a direct push made without opening a merge request, and a squash message hand-edited in the merge dialog. Subjects a merge generates are skipped rather than refused — nobody authored them and they never reach a release note.

The accepted vocabulary and the routing table are held against each other, in both directions: every accepted type must reach a heading of its own rather than falling through to the catch-all, and every heading must be reachable from some accepted type. A disagreement between the two is invisible from either file alone — it surfaces only as an entry quietly filed under the wrong heading, months later — so it fails as a test instead.

What none of this can check is whether a well-formed title is *true*. A title that describes only what the change started as, or carries a type that misdescribes it, passes every gate and still misfiles the entry. That part is discipline, and the release note is where getting it wrong shows up.

## Pre-releases

A pre-release publishes to PyPI exactly as a full release does. The difference is on the consumer's side: an ordinary install resolves to the newest full release and ignores pre-releases entirely, so publishing one cannot disturb anyone who has not asked for it. Someone testing an unreleased change opts in explicitly.

That makes a pre-release the correct way to exercise a change against a real consuming application before committing a version number that ordinary users will receive.

## Related Artifacts

- **Line of truth (self-enforcing):** `.gitlab-ci.yml` (when the publish job runs and what it must pass first); `scripts/release_tag.py` (the tag grammar and the version it implies); `scripts/conventional_commit.py` (the accepted commit-type vocabulary and the subject grammar); `pyproject.toml` (the build backend and where it reads the version from); `cliff.toml` (how commits become the generated half of a release note, and where a release begins); `docker-compose.yml` (the pinned generator).
- **Decisions (why):** ADR-002 records why the repository declares versions in SemVer and lets the build backend normalise, rather than tagging in PEP 440 or deriving the version from the tag. ADR-003 records why there is no changelog file and why a release note is half generated and half written.
- **Related specs:** SPEC-008 (the pipeline this publish stage belongs to, and the checks a release must clear first); SPEC-006 (the registered AniDB client identity, and why it moves independently of this version); SPEC-007 (the toolchain the gate runs under).
- **Tests:** the tag grammar and the artifact check are covered by `tests/unit/test_release_tag.py`. That the declared version is itself a legal tag, and that the build backend normalises it the way the gate predicts, are covered in `tests/unit/test_package.py`. The subject grammar, and the cross-check holding the accepted vocabulary against the routing table in both directions, are covered by `tests/unit/test_conventional_commit.py` — that check is the "fails as a test" this spec relies on.
