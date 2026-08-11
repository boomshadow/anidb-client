---
name: spec
description: 'Executable tooling for Specifications: validate frontmatter, generate docs/specs/INDEX.md, and guide spec lifecycle (create, update, delete). Use after any spec creation, edit, rename, or deletion. Writing rules and philosophy live in docs/specs/SPEC-000-anchored-development.md ("Writing Specs" section).'
---

# Spec Skill

## Writing Rules

Read the **Writing Specs** section of `docs/specs/SPEC-000-anchored-development.md` for all spec formatting, frontmatter, granularity, and lifecycle rules. Do not duplicate them here.

### Project Writing Conventions

Specs in this project describe **observable behavior** — what a caller of the library perceives — not how the Python achieves it. Describe the contract and the semantics; leave the mechanism to the code.

**The test:** if a developer could reasonably change this detail during normal development without needing a spec amendment, it is too specific for the spec. Specs should be stable across routine implementation adjustments.

**Two lines of truth this project does not own.** The AniDB UDP API is an external contract: its command grammar, response codes, and field names are defined by AniDB, not here. Specs may name that vocabulary — response code `330`, the `MYLISTADD` command, the `ed2k` file identifier — because naming it ties the spec to the contract the code must satisfy. What specs must not do is restate the protocol as though this project defined it.

The one artifact this project *does* own that behaves self-enforcingly is the SQLAlchemy declarative schema in `src/anidb_client/db.py`: it is executed to create the cache tables, so the schema is its own definition. Specs point at it rather than re-listing columns, and cover what it cannot express — freshness policy, when a fetch is triggered, what happens on a failed lookup.

**Too prescriptive** — restates the implementation, goes stale on the next refactor:
> `update_if_old()` computes `weeks_old = age // timedelta(weeks=1)`, loops multiplying `refresh_probability` by 1.5, then calls `random.randint(1, 100)` and compares.

**Observable** — describes the contract, with room for implementation change:
> Cached data is never refreshed more than once a day. Past that, the chance of a refresh starts at nothing in the first week and grows with age, so a large cache warms up gradually instead of expiring all at once.

## Lifecycle Operations

### Create

1. Scan `docs/specs/` for existing `SPEC-*.md` files.
2. Find the highest `NNN` number. Use `NNN + 1` for the new spec, zero-padded to three digits. SPEC-000 is reserved for the framework specification.
3. Name the file `SPEC-NNN-short-descriptive-slug.md` (lowercase, hyphens).
4. Include required frontmatter: `title`, `description`, `status`, `tags`.
5. No required body sections. Structure the body to serve the domain — lifecycle phases, scenarios, subsystems, or whatever makes the behavior clearest. Include a Related Artifacts section when relevant cross-references exist (module paths, ADR numbers, related specs, and test locations by convention).
6. Set `status: accepted` unless the spec is a handoff for a future session (use `draft`). Draft is a local/branch-only state — the validator and CI will block merging until all drafts are resolved to `accepted`.
7. Run the validator.

### Update

Edit the spec in place. Never create a "superseded" replacement — this project follows the living-document approach. Git preserves history.

After editing, run the validator.

### Delete

Delete the file. Do not archive or mark superseded. Gaps in numbering are intentional and expected — numbers are never reused.

After deleting, run the validator.

## Validator

### When to Run

After any spec creation, edit, rename, or deletion.

### How to Run

This project is Docker-native — do not install Python or pyyaml on the host. The generator runs inside the dev container, where `pyyaml` is part of the `dev` dependency group:

```bash
task spec-index
```

That wraps `docker compose run --rm dev uv run --frozen python .claude/skills/spec/scripts/generate_index.py`.

### What It Does

1. **Validates** all `SPEC-*.md` files in `docs/specs/` — checks filename format, YAML frontmatter presence, required fields (`title`, `description`, `status`, `tags`), and status values.
2. **Generates** `docs/specs/INDEX.md` — but only if validation passes with zero errors.

Passing `--check` validates and rebuilds the index in memory but writes nothing, failing instead if the committed file is out of date. That is the mode CI runs; `task index:check` runs both checks locally.

### On Failure

If the script exits non-zero, fix the reported errors before proceeding.

**Draft status errors are never pre-existing conditions to ignore.** If the validator reports `status is 'draft' (signals incomplete work)`, the current session must resolve it. Verify the spec reflects current codebase reality, set `status: accepted`, and re-run the validator.
