---
title: "Coding Style and Conventions"
description: "House coding conventions for anidb-client: Docker-native development with no host Python toolchain, the Taskfile as the developer-command entry point, a current-rather-than-conservative interpreter floor restated to ruff, to mypy and in the pinned image tag rather than derived, ruff for formatting and linting, mypy under global strict with its two documented exemptions and repository tooling inside its scope rather than beside it, the data layer written in SQLAlchemy 2.0 idiom rather than 1.x, codespell, the pin-and-verify supply-chain posture (exact pins, hashed uv.lock, a 45-day soak declared to the resolver, a digest-pinned base image), the no-network testing discipline enforced by the suite itself rather than by the runner, and the ratcheting coverage floor."
status: accepted
tags: [coding-style, conventions, python, interpreter-floor, requires-python, target-version, ruff, mypy, strict, type-annotations, scripts, tooling, sqlalchemy, mapped-column, select, codespell, docker-native, taskfile, uv, uv-lock, supply-chain, pin-and-verify, soak, digest-pin, editorconfig, testing, network-guard, fake-server, coverage, coverage-floor, postgres-marker]
---

# Coding Style and Conventions

This spec is the signpost for how code is written and checked here. The enforcing detail lives in self-documenting configs — `pyproject.toml`, `.editorconfig`, `Taskfile.yml`, the `Dockerfile` — which this spec points at rather than restates. It exists because several of these conventions are house rules a capable engineer would not infer from the code alone.

## Docker-native development

Everything runs inside a container. The host is expected to have Docker and [Task](https://taskfile.dev) and nothing else — no Python, no uv, no ruff, no pytest. Because this project is a library with no long-running service, the dev container is one-shot: commands run through `docker compose run --rm`, which the Taskfile wraps.

The Taskfile is the line of truth for developer commands. `task --list` is the discoverable surface, and `task check` runs what CI runs. When a common operation appears, it gets a task.

## The interpreter floor is current, not conservative

`requires-python` names a recent Python rather than the oldest one the code would still run on. The trade is deliberate: supporting a range means either avoiding newer idioms or guarding them, and this package would rather read as current code. Raising the floor is a normal change here, not an event.

That floor is **restated in three places rather than derived**, and knowing where they are is the point of this section.

To ruff as `target-version` and to mypy as `python_version`: ruff infers it from `requires-python` when it is not declared, and the inferred value silently decides which rewrites the `UP` and `FURB` rules propose and whether the formatter emits syntax older interpreters cannot parse — behavior worth being able to see in the config rather than derive.

And, least visibly, in the **tag of the pinned toolchain image**, which the `Dockerfile` and the CI pipeline both name. Nothing derives that tag from `requires-python`, so raising the floor means bumping the image in the same change or the two silently disagree. SPEC-008's reason for having no oldest-interpreter job rests entirely on those two agreeing.

## Formatting, linting and typing

**ruff** is both formatter and linter, configured in `pyproject.toml`. Its rule selection subsumes what would otherwise be several tools; the `pyproject` is authoritative for which rules are enabled.

**mypy is strict, everywhere.** Every module in the package is annotated and checked under `strict`, and a new one is strict from its first line rather than opted in afterwards. There is no per-module exemption list and adding one would be a regression, not a convenience: the point of a single global setting is that no module can be quietly left out of it.

Repository tooling is inside that scope, not beside it. Scripts that are not shipped in the wheel are still checked and still tested, because a script that decides whether a version reaches PyPI is not glue — it is code with consequences, held to the standard of the code it gates.

This was reached rather than declared. The package arrived with no annotations at all, where blanket strictness would have produced hundreds of errors and, inevitably, a blanket ignore that checks nothing — so strictness was applied module by module through a per-module list that could only grow. That list is now the global setting, and the history is recorded here only to explain why a reader will not find one.

Two things stay outside it, each documented where it lives:

- **A third-party library with no type information**, waived in `pyproject.toml`. `libnfs` is the remaining case; it ships nothing and is not installed in the development environment at all, so `nfs://` paths are typed at the boundary and `Any` beyond it. A library that gains type information gets its waiver removed, not kept for tidiness.
- **The library logger**, waived on the declaration itself rather than in configuration, because it is one line rather than a module. It is `None` until `init()` runs, which is a state no code path that logs can observe, so it is annotated as a logger with a single documented `type: ignore` rather than made optional at every call site. The one module a caller's own test can reach before `init()` guards explicitly and has a test for it.

Where the annotations reach the edge of what can be stated — an attribute forwarded to whichever cached row carries it, a conversion table whose every entry produces a different type, an AES cipher object with no common base across modes — `Any` is used deliberately and says in a comment why the alternative would be worse. `Any` that merely postpones the question is not the same thing.

**The data layer is written in SQLAlchemy 2.0 idiom.** Models are declared `Mapped[]` / `mapped_column()`, and rows are read with `select()` executed through the session rather than through `Session.query()`. Nothing forces this: the 1.x forms still work and the query API is explicitly still supported upstream. It is a house rule because the alternative is a codebase in two dialects, where every reader has to know both — and because the legacy declarative style could not be typed honestly, which cost real workarounds before it was migrated. SPEC-003 carries the consequence that matters for the schema: nullability is stated rather than inferred. The rule covers the tests too — they read rows the same way the library does, so there is no second dialect anywhere to learn.

**codespell** checks prose and code alike. Its ignore list is for real words that look like typos — AniDB field names, values quoted from wire payloads — and each entry carries a note saying which. Whole files are skipped only when naming their contents individually would re-introduce the same false positives.

`.editorconfig` is authoritative for indentation, charset and line endings per file type and must be honored. A new file type it does not cover gets a section added rather than ad-hoc formatting.

## Supply-chain posture

Every external input is pinned to an exact version and verified.

- **Dependencies are pinned exact and hash-locked.** Direct dependencies name exact versions in `pyproject.toml`; `uv.lock` carries the resolved set with integrity hashes. Installs run frozen and never resolve at build time, so CI, the container and a developer's machine get identical trees.
- **The 45-day soak is declared to the resolver, not just to the bot.** Renovate's release-age filter governs the updates it *proposes*, but a bot-side filter does not reach a lockfile refresh, which is delegated to the package manager — so transitive dependencies could otherwise land the day they are published. Declaring the window in `pyproject.toml` applies it to every resolution instead. A package that must be held younger than the window gets a dated per-package exception, so the waiver reads as deliberate on inspection rather than persisting silently.
- **The base image is digest-pinned**, to the image index rather than a per-arch child so the right binary is selected per build platform.

## Testing

**The test suite never contacts AniDB.** A fake server on loopback stands in for the API, and an autouse guard fails any test that addresses a non-loopback host. The point worth stating is *where* that guard lives: it is enforced by the tests themselves, not by the container or the CI runner, so it holds on a developer's machine and in any environment the suite is run in. A test that reached the real API would risk an IP ban for whoever ran it next.

Timing is injected, not slept through. The rate limiter takes its clock and its sleep function as parameters so ban back-off — measured in half-hours in production — is tested instantly. The suite carries a per-test timeout well above any legitimate test, so a test that blocks on a real socket or a real sleep fails loudly instead of hanging the run.

**Tests that need a real PostgreSQL are marked, not silently skipped.** SQLite exercises neither the native enum types nor the wide-integer variant the schema declares, so a SQLite-only run covers neither branch. Those tests carry a marker and skip when no server URL is configured — which makes them cheap locally and easy to forget, so CI runs them a second time as an explicit gate that fails if they did not actually execute.

**Coverage has a ratcheting floor.** The configured minimum is a floor to defend, not a target to sit at: it is raised as coverage grows, and set a little under the measured figure so an unrelated change does not fail the build on rounding.

The floor tracks work, not scope. Measurement covers repository tooling as well as the library — a script that decides what reaches PyPI is not exempt from being tested — but widening what is measured raises the figure without anything being better tested, so the floor does not ratchet on that. It moves when coverage is earned.

## Related Artifacts

- **Line of truth (self-enforcing):** `pyproject.toml` (ruff rules, the global mypy strict setting and the one library waived from it, codespell's ignores, pytest configuration and markers, the coverage floor, exact dependency pins and the declared soak window); `uv.lock` (the resolved, hashed dependency set); `.editorconfig` (formatting per file type); `Taskfile.yml` (the developer commands); `Dockerfile` and `docker-compose.yml` (the container the commands run in).
- **Related specs:** SPEC-008 (the CI pipeline that runs these same checks, and the scanning that reads `.grype.yaml`); SPEC-003 (why the PostgreSQL-marked tests exist at all).
- **Tests:** style and typing conformance is enforced by the tooling itself through `task check` rather than by unit tests. The conventions that tooling cannot enforce are covered directly: the network guard's own behavior in `tests/test_network_guard.py`, and the bounded-HTTP-timeout rule — that no `urlopen` call site is left unbounded — in `tests/unit/test_http_timeouts.py`.
