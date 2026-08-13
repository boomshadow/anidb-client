---
title: "CI Pipeline and Security Scanning"
description: "The GitLab CI pipeline for anidb-client across six stages: validate (lockfile freshness plus spec/ADR INDEX freshness), test (ruff, mypy, codespell, pytest against a real PostgreSQL service), build (the wheel and sdist that publish later uploads), security (shared ci-templates Semgrep SAST, Grype dependency scanning and the .grype.yaml exception audit, with a daily rescan schedule), drift-detection (the merge-request-only anchor-watch gate), and publish (tag-only PyPI upload over OIDC trusted publishing, gated behind every earlier stage, with the tag rules themselves owned by SPEC-009). Explains why container scanning is deliberately absent."
status: accepted
tags: [ci, gitlab-ci, pipeline, stages, validate, lockfile, index-freshness, lint, ruff, mypy, codespell, pytest, postgres-service, build, wheel, sdist, security-scanning, semgrep, sast, grype, dependency-scanning, exception-audit, ci-templates, soak, schedule, rescan, drift-detection, anchor-watch, anchored-development, publish, pypi, trusted-publishing, oidc, tags, renovate]
---

# CI Pipeline and Security Scanning

The pipeline exists to make a release trustworthy: everything that reaches PyPI has been linted, typed, spell-checked, tested against a real database, scanned for known vulnerabilities, and — on the way in — checked for documentation drift. This spec describes the pipeline's behavior; `.gitlab-ci.yml` is the line of truth for how it is wired.

## Pipeline shape

Six stages run in order: **validate**, **test**, **build**, **security**, **drift-detection**, **publish**.

A single toolchain image — the same digest-pinned uv image the `Dockerfile` uses — is shared by the jobs that need Python, so a local run and a CI run use an identical toolchain.

### When pipelines run

A merge request gets a merge-request pipeline; a branch without an open merge request gets a branch pipeline; the two never both run for the same commit. Tags get their own pipeline.

Most jobs run on branches, merge requests and tags alike, but never on the security **schedule** — a scheduled pipeline exists only to re-run the scans against fresh vulnerability data, so the project's own jobs opt out of it.

## Validate

**Lockfile freshness.** `uv.lock` must match `pyproject.toml` exactly. Without this check a dependency added to `pyproject` but never locked would install fine locally, where uv resolves on the fly, and then differ from what every other job and every user gets.

**Index freshness.** The spec and ADR indexes are generated artifacts (SPEC-000), so CI rebuilds each one from the frontmatter and fails if it differs from what was committed — which means the author changed a spec or ADR without re-running the generator. The check writes nothing and is performed by the generator itself rather than by diffing the working tree, so it needs no tooling beyond the Python environment the job already has. These jobs are the reason the indexes can be trusted as routing tables.

## Test

**Lint** runs ruff's linter and formatter check and codespell. It additionally emits a GitLab Code Quality report so findings render inline on the merge-request diff; the report is produced unconditionally and the gating checks that follow it are what actually fail the job.

**Typecheck** runs mypy under the global strictness described in SPEC-007.

**Test** runs the full suite with branch coverage against a **real PostgreSQL** stood up as a GitLab service — the same image and digest `docker-compose.yml` uses, so a local run and CI talk to the same server version. Coverage and JUnit results are published to the merge request.

That job then runs the PostgreSQL-marked tests a second time on their own. The marked tests skip silently when no server is reachable, which would make the job quietly weaker than it looks; the second run fails if they did not actually execute.

**There is no oldest-interpreter job.** One used to run here, because the package supported a range of interpreters and the jobs above exercised only the newest of them. The declared floor is now the same interpreter every other job already runs, so a separate job would exercise nothing the others do not. It belongs here again the day the floor and the interpreter CI runs on stop being the same thing.

## Build

The wheel and sdist are built once, in their own stage, and carried forward as artifacts. Publish uploads *those* artifacts rather than rebuilding, so what is released is what was tested. The job asserts that both a wheel and an sdist were produced rather than trusting the build to have done so silently.

## Security

Scanning comes from the shared `ci-templates` project, consumed from its default branch with no pinned ref: those templates roll deliberately, so a soaked scanner-image bump lands here on the next pipeline without a second soak.

- **Semgrep SAST** over the source.
- **Grype dependency scanning** over the locked dependency set.
- **An exception audit** that reads `.grype.yaml` and fails on entries that are undated, expired, or missing a rationale. An empty exception list is the healthy steady state.

A **daily schedule** re-runs only this stage, so a vulnerability disclosed after a release is found without anyone pushing a commit.

**Container scanning is deliberately absent.** This project's deliverable is a wheel on PyPI, not an image. The `Dockerfile` here is a development and test harness that is never published, so scanning it would gate releases on findings in a build tool that ships to nobody. Dependency scanning covers the surface that does reach users.

## Drift detection

**anchor-watch** is the Anchored Development enforcement gate (SPEC-000). It compares the merge request's diff against the project's specs, ADRs, tests and navigation aids and fails the pipeline when they have drifted apart.

It runs on merge requests only, and never on Renovate branches. Drift is a pre-merge gate rather than something to run on every branch push, and a version bump is not a behavioral change to anchor. It runs in a digest-pinned image carrying Node and the Claude Agent SDK, and requires an authentication token supplied as a project CI/CD variable. The job does not allow failure: a drift finding blocks the merge until a human resolves it.

## Publish

Publishing happens only from a version tag, and only after every earlier stage has passed. The job uploads the artifacts the build stage produced, over OIDC trusted publishing, and refuses to upload anything unless the tag and those artifacts agree.

**SPEC-009 owns that behavior** — the tag grammar, what the gate checks, why a malformed tag fails loudly here rather than quietly matching no job at all, and how PyPI's side of the trust is configured. What belongs to the pipeline is only where it sits: the last stage, reachable from a tag alone, and gated behind the whole suite and the scanners rather than running alongside them.

That placement is the point. Everything a release has to survive happens before the one step that cannot be undone.

## Related Artifacts

- **Line of truth (self-enforcing):** `.gitlab-ci.yml` (stages, jobs, rules and images); `.grype.yaml` (scan exceptions and their expiry); `renovate.json` (the dependency-update policy the soak window backs); `pyproject.toml` and `uv.lock` (what the validate and test stages check).
- **Decisions (why):** ADR-001 records why the README remains a full user-facing document, which is what the drift gate is told not to flag as duplication.
- **Related specs:** SPEC-000 (the framework anchor-watch enforces, and the index-generation requirement the validate stage checks); SPEC-007 (the same lint, type, spell and test checks as they run locally, and the supply-chain posture the scanning backs); SPEC-003 (why a real PostgreSQL service is required rather than SQLite); SPEC-009 (what the publish stage actually verifies before it uploads, and the tag rules it enforces).
- **Tests:** the pipeline is verified by running. The one project-side invariant it depends on — that the suite never reaches the network — is covered by `tests/test_network_guard.py`.
