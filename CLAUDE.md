# anidb-client — Object-Oriented UDP Client Library for AniDB

`anidb-client` wraps the AniDB UDP API in ordinary Python objects — `Anime`, `Episode`,
`File`, `Group` — and keeps an aggressive local SQL cache in front of it, because the UDP
API is strictly rate-limited and bans clients that talk to it too often. The typical use is
mylist management: identifying local files by ed2k hash and adding, editing or removing them
from an AniDB mylist. The deliverable is a wheel on PyPI; there is no service and nothing is
deployed.

This file is a **navigation aid**. It routes you to the authoritative artifacts; it does not
duplicate them. When you need to know *what the system does*, read the relevant spec.

## Documentation — Anchored Development

This project follows [Anchored Development](docs/specs/SPEC-000-anchored-development.md):
four interconnected artifact types — code, tests, specs, ADRs — kept anchored to reality.
Read SPEC-000 before writing specs or ADRs; the writing rules live there (with project
conventions in the `spec` and `adr` skills).

- **Specs** (`docs/specs/`, index [`INDEX.md`](docs/specs/INDEX.md)) — behavioral
  expectations by domain. They describe behavior and point at the code.
- **ADRs** (`docs/decisions/`, index [`INDEX.md`](docs/decisions/INDEX.md)) —
  architectural reasoning and rejected alternatives.
- **Lines of truth** — two, and they are different kinds of thing. The **AniDB UDP API** is
  an *external* contract: commands, response codes and field names are AniDB's, transcribed
  here in `src/anidb_client/commands.py` and `responses.py`. The **cache schema** in
  `src/anidb_client/db.py` is *self-enforcing*: it is executed to create the tables, so the
  models are their own definition. When prose disagrees with either, prose is wrong.
- **Drift detection** — the agent at `.claude/agents/drift-detector.md`. On every change,
  evaluate all four artifact types and check for drift.

> **Running the drift detector (Claude Code sessions):** run it as a **subagent** — the
> `Agent` tool with `subagent_type: drift-detector` — after making changes. Never run it as
> a foreground task, and do not use the Node runner locally. That runner
> (`.claude/agents/scripts/ci-drift-detector.mjs`, executed in the pinned anchor-watch image)
> exists only to drive the *same* agent headlessly in CI, where there is no interactive
> session. Locally, you are the session — spawn the subagent directly.

Read the index, not every file — load the full spec/ADR only when a change touches its
domain. Edit artifacts **in place** (living documents); never add "superseded" sections.

**The README is deliberately not trimmed.** ADR-001 records why `README.md` keeps its full
user-facing reference content instead of being reduced to a navigation aid: it is the
package's PyPI long description. Overlap between it and the specs is intended. The specs
hold authority — when the two disagree, fix the README.

### Domain map — read the spec before changing the code

| Domain | Spec |
|--------|------|
| Object layer & attribute resolution — `Anime`/`Episode`/`File`/`Group` construction from a title, id or path; lazy resolution through in-memory value → cache → network, with falsy cached values counting as answers; unknown attributes answering `None` rather than raising, while a fetch that cannot be answered at all raises the reason — banned, timed out, refused — on a bounded wait rather than blocking; illegal objects for ids AniDB does not recognise (and the rule that becoming illegal must never block the waiter); `update()` as the forced refresh; transitive `related_anime()` walking with `exclude` walls and the `only_in_mylist` bound over a cyclic relation graph; equality/containment semantics; cover-image download | [SPEC-001](docs/specs/SPEC-001-object-layer-and-attribute-resolution.md) |
| UDP session & rate limiting — session establishment with the registered client identity and optional AES-encrypted sessions (a configured key makes encryption mandatory); a handshake whose success is recognised by a whitelist and which always settles, with refused credentials latched and never re-sent while a ban is retried after a back-off, and the reported NAT address treated as advisory rather than as something that can fail the login; sender/listener thread pair, the order they start in, and the state they share through locked accessors (pending commands, session key, cipher, pacing counters), with the pacing lock never held across a back-off sleep; a sender that waits to be woken rather than polling; a callback thread per reply and why it is not a pool; monotonic timing for every interval measured; tag-based correlation over an unordered datagram protocol; outbound pacing (short burst then steady rate, application time counted toward the interval, burst allowance refreshed after idle); exponential jittered ban back-off with a ceiling, cleared by a successful auth, held as a window during which nothing is sent rather than as a sleep -- so the listener keeps reading and a caller is told rather than held; session-loss detection and re-auth with the discovering command re-queued at the front; command timeout with an attempt budget that is spent rather than renewed (auth and encrypt back off instead); every request carrying an outcome — the reply, or the reason there will not be one — settled after its callback has run, and a transport that fails every caller it was working for rather than stopping silently; NAT keepalive and idle session refresh; the two containment rules — a library never terminates its host process, and a waiter is always released | [SPEC-002](docs/specs/SPEC-002-udp-session-and-rate-limiting.md) |
| Caching & the local data model — the SQL cache (SQLite/PostgreSQL/MySQL) and the on-disk XML caches; the freshness policy (hard one-day floor, freshness roll at most once per 20 hours so a daily cron still gets a decision, age-weighted probability starting at nothing in week one, anime-specific bonus decaying from AniDB's own last-change proximity); 36-hour XML refresh with fall-back-to-stale on a failed fetch and a size sanity check; what the schema guarantees (dual timestamps, relations as reconciled rows, database-level enum vocabularies defined once and selected from by the wire layer rather than restated, wide-integer SQLite variant, generic file rows, nullability stated on every column rather than inferred from its annotation); the block-scoped lifetime of a cache session, so a failure or an early return still returns the connection; how the engine is opened for a threaded process (WAL attempted with the obtained mode read back and logged, an explicitly chosen busy timeout, foreign keys enforced on every SQLite connection, a bounded connection pool whose size `init()` exposes, and pragmas that fire for SQLite alone); best-effort cache writes; file cache identity re-validated by size then size+ed2k; **no migration story — recreate the database after upgrading**, which is also what makes enforcing foreign keys safe, and the per-backend DDL snapshot that makes an unintended schema change a build failure rather than a cache rebuild for every user | [SPEC-003](docs/specs/SPEC-003-caching-and-the-local-data-model.md) |
| File identification & mylist — size+ed2k as the identity AniDB recognises, hash reuse gated on size and mtime, ed2k degenerate cases; generic entries when AniDB has never seen the file; two-part inference (anime from parent directory at the normal threshold then from a stripped filename at a deliberately lower one; episode from an ordered regex ladder whose fallbacks are gated behind the single-episode test); AniDB's special/opening/ending/trailer numbering with the halfway-through-credits ending guess; multi-episode ranges (filename parsing supports them, the file API does not) and part files; mylist add/edit via one entry point, the one-entry-per-episode rule, per-episode adds for generic files, local-first edits vs. add-then-read, a write the transport cannot deliver raising rather than reporting a change that never happened; removal clearing cached state regardless of reply; promotion of a generic entry once AniDB learns the real file | [SPEC-004](docs/specs/SPEC-004-file-identification-and-mylist.md) |
| Title matching & external mapping — fuzzy scoring against AniDB's anime-titles export with the threshold as a parameter (lowered for filename-derived guesses) and only the top result becoming an `Anime`; unresolvable title ambiguity; ISO-639 language normalisation; Anime-Lists mapping to TVDB (tv only) / TMDB (tv and movie) / IMDB (movie only) with list-valued movie ids selected per episode; episode mapping's three mechanisms in priority order (per-episode map, start/end range with offset, anime-level default season+offset), season-zero banding for specials, episodes mapped to zero meaning no mapping, part-number and list-valued results, and the movie-parts ragged edge; fanart.tv's two preconditions, empty-list-not-raise, and partial-result handling | [SPEC-005](docs/specs/SPEC-005-title-matching-and-external-mapping.md) |
| Configuration & credentials — `init()` as the single entry point and the database URL as its only required argument, with an in-memory SQLite URL refused outside `db_only` and the cache pool's size exposed as an argument; three credential arrangements (direct, netrc, or `db_only` which must not demand them); exact netrc machine-name sets for AniDB credentials, database credentials (hostname only — no port, no IPv6 brackets, case-insensitive) and the fanart key; database password injection only when the URL has none, only when the credential belongs to the URL's user, rebuilt structurally with percent-encoding rather than string surgery; registered client identity deliberately unrelated to the distribution version; encryption key; per-call random outgoing UDP port; logging setup with AUTH contents never logged; the per-socket-operation HTTP timeout; `close()` | [SPEC-006](docs/specs/SPEC-006-configuration-and-credentials.md) |
| Coding style & conventions — Docker-native development with no host Python toolchain and the Taskfile as the developer-command entry point; a current-rather-than-conservative interpreter floor, restated to ruff and mypy and carried in the pinned image tag rather than derived; ruff for format+lint; **mypy under global `strict`** across every module, with the two exemptions it does keep named and reasoned in `pyproject.toml`; codespell and its "real words that look like typos" ignore list; pin-and-verify supply chain (exact pins, hashed `uv.lock`, the 45-day soak declared to the *resolver* not just to Renovate, digest-pinned base image by index); the no-network test discipline enforced by the suite itself rather than the runner; injected clocks; PostgreSQL-marked tests re-run as an explicit gate so a silent skip cannot pass; the ratcheting coverage floor | [SPEC-007](docs/specs/SPEC-007-coding-style-and-conventions.md) |
| CI pipeline & security scanning — six stages (validate/test/build/security/drift-detection/publish); lockfile and spec/ADR INDEX freshness gates; lint/typecheck/test against a real PostgreSQL service; wheel+sdist built once and carried to publish; shared `ci-templates` Semgrep/Grype/exception-audit consumed from their default branch with a daily rescan schedule; **no container scanning** (the deliverable is a wheel, the image is a harness); MR-only, no-Renovate `anchor-watch` drift gate that does not allow failure; tag-only PyPI publish over OIDC trusted publishing with a tag-versus-wheel version check | [SPEC-008](docs/specs/SPEC-008-ci-pipeline-and-security-scanning.md) |

## Working Agreement

### Docker-Native Development

Everything runs inside Docker. The host needs Docker and [Task](https://taskfile.dev) and
nothing else — no Python, no uv, no ruff, no pytest. Because this project is a library with
no long-running service, the dev container is one-shot: `docker compose run --rm dev`, which
the Taskfile wraps.

```bash
# YES — through the Taskfile, which runs in the container
task test
task check

# NO — never on the host
pytest
uv run mypy
```

Simple filesystem operations (ls, cat, mkdir) and docker compose / Task itself are the only
things that run directly on the host.

### Taskfile

`Taskfile.yml` is the line of truth for developer commands — run `task --list` to discover
them. `task check` runs what CI runs. Eat your own dog food: use the Taskfile yourself, and
when you add a common operation, add a task for it.

### Never Contact AniDB from a Test

The suite fakes the API on loopback and an autouse guard fails any test that addresses a
non-loopback host. Keep it that way — a test that reaches the real API risks an IP ban for
whoever runs it next. This is enforced by the tests themselves, so it holds on your machine
too, not just in CI.

### EditorConfig and GitIgnore

Honor `.editorconfig` and `.gitignore` at all times. Read them before writing code. If you
introduce a new file type not covered by `.editorconfig`, add a section for it. If you
introduce files or directories that should not be committed, add them to `.gitignore`.

### Dependencies

Exact pins, hash-locked in `uv.lock`, 45-day soak. Never loosen a pin or bypass the lock.
See SPEC-007 before touching `pyproject.toml`'s dependency sections.

## Project Structure

```
src/anidb_client/
  __init__.py       init(), the public surface, HTTP helpers   → SPEC-006/SPEC-001
  animeobjs.py      Anime/Episode/File/Group + lazy resolution → SPEC-001/SPEC-004
  link.py           UDP transport, session, listener threads   → SPEC-002
  ratelimit.py      Outbound pacing and ban back-off           → SPEC-002
  commands.py       AniDB command set + parameter validation   → SPEC-002
  responses.py      AniDB response-code table and parsing      → SPEC-002
  db.py             Cache schema (self-enforcing)              → SPEC-003
  anames.py         anime-titles / anime-list XML + matching   → SPEC-005
  mapper.py         Field converters and request bitmasks      → SPEC-005/SPEC-002/SPEC-003
  fileinfo.py       ed2k hashing, file stats, filename regexes → SPEC-004
  errors.py         Exception hierarchy
tests/unit/         The bulk of the suite
tests/integration/  Needs a real PostgreSQL or the fake server
tests/fake_anidb.py Loopback stand-in for the AniDB UDP API    → SPEC-007
tests/schema_snapshot.py    Renders the schema as DDL          → SPEC-003
tests/schema_snapshots/     The stored DDL, per backend        → SPEC-003
docs/specs/         Behavioral specs + auto-generated INDEX
docs/decisions/     ADRs + auto-generated INDEX
```
