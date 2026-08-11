---
name: adr
description: 'Executable tooling for Architecture Decision Records: validate frontmatter/body, generate docs/decisions/INDEX.md, and guide ADR lifecycle (create, update, delete). Use after any ADR creation, edit, rename, or deletion. Writing rules and philosophy live in docs/specs/SPEC-000-anchored-development.md ("Writing ADRs" section).'
---

# ADR Skill

## Writing Rules

Read the **Writing ADRs** section of `docs/specs/SPEC-000-anchored-development.md` for all ADR formatting, frontmatter, body structure, and lifecycle rules. Do not duplicate them here.

### Project Writing Conventions

ADR Decision sections document specific technical choices — naming the tool, the pattern, the trade-off, and the alternatives that were rejected and why. That specificity is the point; it is what prevents re-litigation.

**Write an ADR when there is a real decision to record, not to populate the directory.** An ADR that restates what the code obviously does, or that reasons about a choice nobody would question, is overhead pretending to be rigor. The guiding question in SPEC-000 is the bar: would a capable engineer new to this codebase likely propose a different approach, and would that be a problem? If not, there is no ADR to write. A small number of ADRs is a healthy steady state for a project this size.

Consequences sections should describe what changes for the team and the system's behavior, not restate implementation details. Avoid claims about the current state of other files (e.g., "`.gitlab-ci.yml` is unchanged") — those go stale. Instead, describe the structural outcome.

There is no "Deviations" section. If implementation diverged from the original plan, edit the Decision and Consequences in place to reflect reality. A first-time reader should see one coherent description of the current decision, with no reconciliation burden. The audit log is git.

This same "don't go stale" instinct applies to the **whole ADR**, not just cross-file claims. Write in the present tense of the current decision, not the history of how it got there — point-in-time words ("reverted", "recently", "previously", "now", "as of") are accurate for a week and misleading in a year; git holds the chronology. And when a decision **reverses**, fold the abandoned approach into the Decision's rejected-alternatives, described as *evaluated and rejected with the evidence that settled it* — not as something the system "used to do" or "rolled back." It is correct, not dishonest, for the ADR to then read as a decision to reject X rather than a history of adopting and then removing it: an evidence-backed rejected-alternative answers "why not X?" for a future reader who never saw the experiment, preventing re-litigation far better than a note that X was tried and removed. The evidence that settled it MUST remain. See the "Living Documents" section of SPEC-000.

An ADR that describes domain *behavior* (state machines, protocols, lifecycles) rather than *reasoning* is really a spec — put the behavior in the relevant `docs/specs/` spec and keep the ADR to the why, cross-referencing the spec by number.

## Lifecycle Operations

### Create

1. Scan `docs/decisions/` for existing `ADR-*.md` files.
2. Find the highest `NNN` number. Use `NNN + 1` for the new ADR, zero-padded to three digits.
3. Name the file `ADR-NNN-short-descriptive-slug.md` (lowercase, hyphens).
4. Include required frontmatter: `title`, `description`, `status`, `tags`.
5. Include required body sections: **Context**, **Decision**, **Consequences**.
6. Set `status: accepted` unless the ADR is a handoff for a future session (use `draft`). Draft is a local/branch-only state — the validator and CI will block merging until all drafts are resolved to `accepted`.
7. Run the validator.

### Update

Edit the ADR in place. Never create a "superseded" replacement — this project follows the living-document approach. Git preserves history.

After editing, run the validator.

### Delete

Delete the file. Do not archive or mark superseded. Gaps in numbering are intentional and expected — numbers are never reused.

After deleting, run the validator.

## Validator

### When to Run

After any ADR creation, edit, rename, or deletion.

### How to Run

This project is Docker-native — do not install Python or pyyaml on the host. The generator runs inside the dev container, where `pyyaml` is part of the `dev` dependency group:

```bash
task adr-index
```

That wraps `docker compose run --rm dev uv run --frozen python .claude/skills/adr/scripts/generate_index.py`.

### What It Does

1. **Validates** all `ADR-*.md` files in `docs/decisions/` — checks filename format, YAML frontmatter presence, required fields (`title`, `description`, `status`, `tags`), status values, and required body sections (`Context`, `Decision`, `Consequences`).
2. **Generates** `docs/decisions/INDEX.md` — but only if validation passes with zero errors.

Passing `--check` validates and rebuilds the index in memory but writes nothing, failing instead if the committed file is out of date. That is the mode CI runs; `task index:check` runs both checks locally.

### On Failure

If the script exits non-zero, fix the reported errors before proceeding.

**Draft status errors are never pre-existing conditions to ignore.** If the validator reports `status is 'draft' (signals incomplete work)`, the current session must resolve it. Verify the ADR reflects current codebase reality, set `status: accepted`, and re-run the validator.
