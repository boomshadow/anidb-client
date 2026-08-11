---
title: "Anchored Development"
description: "A framework for living documentation in AI-assisted software development. Defines four interconnected artifact types (code, tests, specs, ADRs), three enforcement modes (self-enforcing, verified, unverified), and the practices that keep documentation anchored to reality. Addresses documentation drift, spec sprawl, and the disconnect between product and engineering teams."
status: accepted
version: 2.0.0
tags: [anchored-development, spec-driven-development, living-documentation, drift-detection, framework]
license: CC-BY-SA-4.0
---

# Anchored Development

> **Documentation that has no consumer and no enforcement mechanism is documentation that will lie to you.**

This is the maxim of Anchored Development. Every practice in this framework follows from it.

## What Is Anchored Development?

Anchored Development is a framework that anchors your codebase to how your system actually works. It defines four interconnected artifact types — code, tests, specs, and ADRs — and the practices that prevent them from drifting apart.

The framework is designed for teams using AI-assisted development, where documentation serves as both human reference and AI context. It is tool-agnostic, language-agnostic, and scales from solo developers to enterprise teams.

Anchored Development sits in the practical middle ground between "no documentation" (everything is tribal knowledge) and "documentation everywhere" (the markdown monster). It provides enough structure to keep documentation trustworthy, without creating overhead that teams abandon.

This specification is versioned and intended for universal adoption. Include it in your repository at `docs/specs/SPEC-000-anchored-development.md`. It is both the standard and the first spec in any adopting project.

## The Problem

Documentation drifts. This is the oldest problem in software engineering, and AI-assisted development has made it both more important and more solvable.

**More important** because AI agents consume documentation as context. When an AI reads an outdated spec and generates code based on stale information, the resulting code is confidently wrong. Bad documentation does not just confuse humans — it actively corrupts AI output.

**More solvable** because the same AI capabilities that consume documentation can also detect when it has drifted from reality. Enforcement mechanisms that were impractical with human-only workflows become lightweight with AI assistance.

Without a framework, documentation degrades in predictable ways:

- **Drift**: The spec says one thing, the code does another. Nobody notices until a newcomer — human or AI — trusts the spec and builds on a false foundation.
- **Sprawl**: Every feature gets its own spec file. After three months there are 47 spec files, 30 of which describe behavior that has been revised twice since. This is the markdown monster.
- **Archaeology**: Documentation becomes a record of what was true at a point in time, not what is true now. Reading it requires forensic reconstruction.
- **Disconnect**: Product defines intent in PRDs. Engineering implements in code. The PRD goes stale. Six months later, nobody can answer "what does the system actually do?" without reading the source.

Anchored Development addresses all four by ensuring every document has a consumer and an enforcement mechanism.

## Principles

### Foundation

**1. Every artifact must have a consumer.**

Something must read it — a human, an AI agent, a runtime, a compiler, a CI check. An artifact with no consumer has no feedback loop. Without a feedback loop, it drifts from reality. Once it drifts, it becomes actively harmful — worse than no documentation at all, because it is trusted documentation that is wrong.

**2. Every document comes in three enforcement modes.**

- *Self-enforcing*: The consumer is the executor. Drift is structurally impossible. Protobuf definitions, database migrations, type systems, SKILL.md files in agentic applications — these cannot lie because the system will not run if they are wrong.
- *Verified*: Not self-enforcing, but tests and CI catch drift. Domain behavioral specs and ADRs live here. Reliable when enforcement tooling is in place.
- *Unverified*: Nothing catches drift. Stale READMEs, orphaned design documents, orphaned PRDs. These are future lies. Delete them or promote them to a stronger enforcement mode.

**3. Favor the strongest enforcement mode available.**

If you can express something as a protobuf definition, a database migration, or a type definition — do that. It is self-enforcing and needs no maintenance. If you cannot (state transitions, timing semantics, error handling, business logic), write a verified spec with test cross-references and drift detection. Never leave an artifact unverified. If it cannot be enforced, it should not exist.

### Structure

**4. Four interconnected artifact types.**

Code (how the system works), Tests (proof that it works correctly), Specs (what the system should do), and ADRs (why the system is built this way). These form a feedback loop — any change to one can ripple to any other. All four MUST be evaluated on every change. Not all four will change every time, but all four MUST be checked.

**5. All artifacts are living documents.**

Edit in place. Git is the history. The document always describes how the system works right now. No "superseded," no "deprecated," no archaeological sediment. A developer, product manager, or AI agent reading a spec or ADR MUST be able to trust that it describes how the system works right now — not how it worked three decisions ago.

**6. Self-enforcing artifacts trump prose.**

Protobuf definitions define the API contract. Migrations define the schema. Type definitions define the data model. Domain specs MUST NOT duplicate what self-enforcing artifacts already express. They cover only what self-enforcing artifacts cannot: state transitions, timing semantics, error handling, retry behavior, business logic. When prose and a self-enforcing artifact disagree, the self-enforcing artifact is correct.

**7. Prose stays above the implementation.**

Specs describe behavior; ADRs describe reasoning. Neither describes implementation — that is the code's job, and the code is its source of truth. Keep prose at the altitude the code does not own: name *what* the system does and *why*, never the line numbers, functions, methods, or internal mechanics that carry it out. That detail belongs to the code; repeating it in prose manufactures the exact drift this framework exists to prevent, because every refactor that leaves behavior unchanged still forces a documentation edit. The test: if a change to *how* the system works, with no change to *what* it does, would force a documentation update, the document was written at too low an altitude. Reference an artifact by its stable name or number — a proto, a migration, a sibling spec — never by a line or an internal symbol.

### Discipline

**8. Domain-level, not feature-level.**

Specs are organized by domain (job lifecycle, billing, authentication), not by feature or ticket (add heartbeat timeout, fix retry bug). Features are absorbed into domain specs when implemented. Domains persist across the life of the system. Features come and go. This is the anti-sprawl mechanism — the total number of specs grows slowly with the system's domains, not linearly with its feature count.

**9. Enforcement is not optional.**

Drift detection MUST run on every change — whether through a CI pipeline, a git hook, or any automated check the team chooses. This is the minimum viable harness. It is what makes verified ADRs, specs, and tests trustworthy. Start from the first commit if you can. If you are adopting into an existing codebase, start now — every change checked from this point forward builds the habit.

### Collaboration

**10. Spec is the shared language.**

Specs are written in natural language, readable by every stakeholder — product managers, architects, engineers, AI agents. When a product manager asks "what happens when a user cancels mid-checkout?", the answer is in the spec — not buried in code or in a ticket from last quarter. The spec is the ongoing reference for how the system behaves, accessible to everyone regardless of technical depth.

**11. Transient artifacts are consumed, not maintained.**

PRDs, tickets, and user stories are communication kickoffs. They initiate work and convey intent. The spec captures the durable behavioral outcome. Once intent is absorbed into the spec, the transient artifact has served its purpose. It can be closed, archived, or deleted. It does not need maintenance — the spec does.

## The Four Artifact Types

Software projects produce four types of persistent artifacts. Each answers a different question and serves a different audience. Together they form a feedback loop where changes to any one can ripple to any other.

### Code

**What it answers**: How does the system work?

Code is the implementation. It is what actually runs. When all other artifacts disagree, the code wins.

### Tests

**What it answers**: Does the system work correctly?

Tests are automated verification. They prove the code does what the spec says it should. When behavior changes, tests break — creating a signal that other artifacts may need updating.

### Specs

**What it answers**: What should the system do?

Specs are behavioral expectations, written in natural language, organized by domain. They describe expected system behavior under specific conditions without prescribing implementation. Specs live at `docs/specs/`.

### ADRs

**What it answers**: Why is the system built this way?

Architecture Decision Records capture the reasoning behind significant decisions — especially rejected alternatives. They prevent re-litigation of decisions by future engineers or AI agents who might otherwise propose an approach that was already considered and dismissed. ADRs live at `docs/decisions/`.

### The Feedback Loop

| When this changes... | Evaluate these for updates |
| -------------------- | -------------------------- |
| Code | Tests, Specs, ADRs |
| Tests | Specs, Code |
| Specs | Code, Tests, ADRs |
| ADRs | Specs, Code, Tests |

The drift detection mechanism automates this evaluation.

## Navigation Aids

Not everything in a repository is an artifact. Repositories also contain documents whose purpose is to route readers to artifacts — READMEs, AI entry files, indexes, changelogs, onboarding guides, etc. These are navigation aids.

The distinction is directional. The four artifact types form a bidirectional feedback loop — a change to any one can drive changes to any other. Navigation aids are always downstream. An index changes because a spec changed. A README changes because the project structure changed. But it never flows the other way — a navigation aid does not drive changes to code, tests, specs, or ADRs.

Navigation aids are subject to the same maxim as artifacts. A stale README wastes a developer's first hour. A stale AI entry file corrupts every AI interaction that follows. Navigation aids must have consumers and enforcement mechanisms.

### Indexes

Each artifact directory MUST contain an auto-generated `INDEX.md`:

- `docs/specs/INDEX.md`
- `docs/decisions/INDEX.md`

Indexes are routing tables for AI context management. An AI agent reads the index to decide which full documents to load, rather than loading every spec or ADR into context.

Indexes MUST be auto-generated from frontmatter — never hand-maintained. The source of truth is always the frontmatter in each individual file. Index generation MUST be run after any spec or ADR change.

Indexes SHOULD be formatted as a markdown table:

```markdown
| Number | Title | Description | Status | Tags |
|--------|-------|-------------|--------|------|
| SPEC-000 | Anchored Development | A framework for... | accepted | framework |
| SPEC-001 | Job Lifecycle | Behavioral expectations for... | accepted | jobs |
```

### Entry Files

Entry files are the documents that humans and AI agents encounter first. READMEs serve humans. AI context files (AGENTS.md, CLAUDE.md, .cursorrules, or equivalent) serve AI agents.

Entry files SHOULD contain a brief reference pointing to the framework and project artifacts:

```markdown
## Documentation — Anchored Development

This project follows [Anchored Development](docs/specs/SPEC-000-anchored-development.md).

- **Specs**: `docs/specs/` — behavioral expectations by domain
- **ADRs**: `docs/decisions/` — architectural reasoning and rejected alternatives
- **Drift detection**: [team's tool or agent location]
```

Entry files route readers to artifacts — they do not duplicate them.

## The Three Enforcement Modes

Every artifact in a repository falls into one of three enforcement modes. The goal is to push every artifact toward the strongest mode available.

### Self-Enforcing

The consumer of the artifact is also its executor. Drift is structurally impossible because the system will not function if the artifact is wrong.

| Example | Why it is self-enforcing |
| ------- | ------------------------ |
| Protobuf / gRPC definitions | Generated code IS the API contract. The runtime enforces it. |
| Database migrations | The database executes the migration. The schema IS the migration's output. |
| Type definitions | The compiler enforces them. |
| SKILL.md in agentic applications | The AI agent reads and executes the skill. The spec IS the runtime instruction. |
| Infrastructure as Code | The infrastructure IS what the code defines. |

Self-enforcing artifacts define the **vocabulary** of the system. They MUST NOT be duplicated in prose.

### Verified

The artifact is not self-enforcing, but tests and CI catch drift when it occurs.

| Example | Verification mechanism |
| ------- | ---------------------- |
| Domain behavioral specs | Tests verify described behaviors. Drift detection checks consistency on every change. |
| ADRs | Drift detection checks whether changes contradict documented decisions. |

Verified artifacts define the **semantics** of the system — the state transitions, timing, error handling, and business logic that self-enforcing artifacts cannot express.

### Unverified

Nothing catches drift. The artifact will diverge from reality and eventually become actively harmful.

| Example | Why it drifts |
|---------|-------------|
| Stale READMEs | No enforcement, no consumer. |
| Orphaned design documents | Written once, never connected to specs or tests. |
| Per-feature spec files | Accumulate, become archaeological. |
| PRDs that outlive their purpose | Communication artifact mistaken for documentation. |

Unverified artifacts MUST be either promoted to verified (by adding enforcement) or deleted. There is no acceptable steady state for unverified documentation in an Anchored Development project.

## Writing Specs

### File Location and Naming

Specs live at `docs/specs/`. Files follow the naming pattern:

```txt
SPEC-NNN-short-descriptive-slug.md
```

- `NNN` is zero-padded to three digits: `000`, `001`, `042`.
- Numbering is append-only — new specs MUST use a number higher than the highest currently in the directory. Numbers are never reused. Gaps in numbering indicate deleted specs and are expected. Git history preserves the original content.
- The slug uses lowercase and hyphens. It should convey the domain at a glance.

`SPEC-000` is reserved for this framework specification.

### Frontmatter

Every spec MUST begin with YAML frontmatter:

```yaml
---
title: "Short human-readable title"
description: "One to two sentences written for retrieval. Include the domain
  covered, the key behaviors described, and enough keywords for search-based
  discovery."
status: accepted
tags: [domain, behavior, relevant-keywords]
---
```

**`title`** — Required. Short, descriptive.

**`description`** — Required. The most important field. Written as a search snippet, not a chapter heading. This is what AI agents and index generators read to decide whether to load the full file. If the description does not contain the right keywords, the spec will be skipped when it should be read.

A description like `"Worker lifecycle stuff"` is unfindable. A description like `"Behavioral expectations for worker registration, heartbeat, job assignment, shutdown, and disconnection handling"` enables precise discovery.

**`status`** — Required. Two valid values:

- `draft` — Proposed but not yet validated against the codebase.
- `accepted` — Validated and authoritative. The only terminal state.

A draft spec MUST be taken to `accepted` during the work that implements or validates it. Draft is a handoff signal, not a permanent state.

**`tags`** — Required. Free-form list of lowercase keywords. No controlled vocabulary — tags are flexible to allow meaningful terms relevant to each document. Tags aid discovery and give AI agents secondary filters beyond the description.

These four fields are the minimum required set. Teams MAY add additional frontmatter fields as needed.

### Body Structure

Unlike ADRs, specs do not require rigid section headers. The body is a behavioral description — what the system should do in this domain, written in natural language.

Structure SHOULD serve the domain:

- Some specs organize by **lifecycle phase** (Registration, Heartbeat, Assignment, Shutdown).
- Some organize by **scenario** (happy path, error cases, edge cases).
- Some organize by **subsystem** or component interaction.

The right structure is the one that makes the domain's behavior clearest to a reader encountering it for the first time.

Every spec SHOULD include a **Related Artifacts** section with lightweight cross-references to relevant self-enforcing artifacts (protobuf files, migration directories), ADRs, and test locations. See the Cross-Referencing section for conventions.

### Domain-Level Granularity

Specs are organized by domain, not by feature. A domain is a cohesive area of system behavior that persists across the system's lifetime.

When a feature is implemented, its behavioral expectations are absorbed into the relevant domain spec. The domain spec grows with the system. The total number of specs grows slowly with the number of system domains — not linearly with the number of features, tickets, or sprints.

This is the anti-sprawl mechanism.

### When to Write a Spec

The guiding question: *"Does this domain have behavioral expectations that cannot be fully expressed by self-enforcing artifacts alone?"* If yes, write the spec.

Write a spec when:

- The domain involves state transitions, timing, or lifecycle behavior.
- Error handling, retry logic, or failure modes need documenting.
- Business rules govern behavior beyond what types and schemas express.
- Multiple engineers or AI agents will work in the domain and need shared behavioral context.

Skip a spec when:

- The domain's behavior is fully expressed by its protobuf definitions, type system, and tests.
- The code is simple enough that tests alone convey the expected behavior.

### Relationship to Self-Enforcing Artifacts

Specs MUST NOT duplicate what self-enforcing artifacts already express. If a protobuf file defines the API contract (RPC signatures, message shapes, enum values), the spec does not re-describe them.

The spec covers what self-enforcing artifacts cannot express:

- **State transitions** — What triggers each state change? What happens on invalid transitions?
- **Timing and liveness** — Heartbeat intervals, timeout thresholds, retry backoff strategies.
- **Error handling** — What happens on failure, disconnection, or resource exhaustion?
- **Business logic** — Priority scheduling, worker selection, access control rules.
- **Side effects** — Cleanup behavior, logging expectations, notification triggers.

The self-enforcing artifact defines the vocabulary. The spec defines the grammar and semantics.

This is the spec's *altitude* (Principle 7): it captures what the system does and under what conditions, not how the code does it — describe behavior, never the mechanics.

## Writing ADRs

### File Location and Naming

ADRs live at `docs/decisions/`. Files follow the naming pattern:

```
ADR-NNN-short-descriptive-slug.md
```

- `NNN` is zero-padded to three digits: `000`, `001`, `042`.
- Numbering is append-only — new ADRs MUST use a number higher than the highest currently in the directory. Numbers are never reused. Gaps in numbering indicate deleted ADRs and are expected. Git history preserves the original content.
- The slug uses lowercase and hyphens. It should convey the decision at a glance.

### Frontmatter

Every ADR MUST begin with YAML frontmatter following the same schema as specs:

```yaml
---
title: "Short human-readable title"
description: "One to two sentences written for retrieval. Include the problem
  domain, the decision made, and at least one major rejected alternative."
status: accepted
tags: [domain, decision, relevant-keywords]
---
```

The description MUST contain the decision made AND at least one rejected alternative.

Good: `"Adopted PostgreSQL as the sole data store with a hand-rolled priority queue. Redis was rejected due to querying limitations for REST API endpoints."`

Poor: `"Database decision."`

These four fields are the minimum required set. Teams MAY add additional frontmatter fields as needed.

### Body Structure

ADRs MUST contain at minimum:

**Context** — What problem exists. What constraints apply. Be specific — do not assume the reader has the same context as the decision maker.

**Decision** — What was decided and why. Lead with the choice, then the reasoning. Rejected alternatives MUST be named with explanations for why they were rejected. This is the section that prevents re-litigation.

**Consequences** — What follows from this decision, both positive and negative. Note any follow-on decisions this creates.

ADRs SHOULD reference related specs and self-enforcing artifacts by number or path — e.g., "See SPEC-001" or "See `proto/v1/job.proto`."

Like specs, ADRs have an *altitude* (Principle 7): they record the decision and its reasoning, not the code that implements it — cite an artifact by name or number, never a line number or a specific function.

### When to Write an ADR

The guiding question: *"Would a capable engineer, new to this codebase, likely propose a different approach here — and would that be a problem?"* If yes, write the ADR.

Write an ADR when:

- You debated alternatives and the reasoning is not obvious from the code.
- You rejected an approach that a reasonable engineer or AI agent might propose.
- The decision has downstream consequences that future work needs to understand.

Skip an ADR for:

- Bug fixes and small tweaks.
- Implementation details obvious from the code.
- Decisions where the spec's context is sufficient.

## Living Documents

All artifacts are living documents.

Traditional ADRs are immutable historical records — when a decision changes, the old ADR is marked "superseded" and a new one is created. Anchored Development treats ADRs as living rationale. When a decision changes, the ADR is edited in place to reflect the current reasoning. Outdated ADRs are deleted, not archived. Git provides the history that immutability was designed to preserve.

Specifications get the same treatment. Left to themselves, specs drift: the code moves on and the document is not kept in step, until it describes a system that no longer exists. Whether that drift is neglect or intent, the result is the same — documentation no one can trust. Anchored Development treats a spec as a living behavioral contract, edited in place as behavior changes, so it always describes the system as it is today rather than as it once was.

A living document is written in the timeless present of the current state, not as a history of how it got there. Point-in-time language — "reverted," "recently," "previously," "earlier," "now," "as of" — is accurate for a week and misleading in a year; it is the drift that living documents exist to prevent. Git holds the chronology; the document does not narrate it. This governs a document's account of its own changes, not behavior that legitimately involves time: a spec may describe a migration performed on upgrade, or a theme that follows live OS changes until the user chooses — that is present-tense behavior, not point-in-time narration. The prohibition targets language pinned to a moving present, not the plain past tense of a settled decision: "X was chosen" or "Y was rejected" state facts that stay true, and are fine.

ADRs carry a further obligation, because they alone record decisions and their rejected alternatives. When a decision reverses, the abandoned approach moves into the Decision's rejected-alternatives, described in the present tense as evaluated and rejected, carrying the evidence that settled it — not narrated as something the system "used to do," "reverted," or "rolled back." The resulting ADR reads as a decision to reject X, not as a history of adopting and then removing it; this is correct, not dishonest, because a rejected-alternative backed by evidence answers "why not X?" for a future reader who never saw the experiment, and that prevents re-litigation far better than a note that X was tried and removed. That evidence — including any production measurement that motivated the reversal — MUST remain in the ADR; it is what lets the rejection stand on its own.

## Drift Detection

Drift detection is the enforcement mechanism that makes verified artifacts and navigation aids trustworthy.

### Purpose

The drift detector compares changes against all four artifact types and navigation aids. It identifies when code changes alter behavior documented in a spec without updating the spec, when changes contradict a decision documented in an ADR, when specs describe behavior not covered by tests, and when navigation aids no longer accurately reflect the artifacts they reference.

The drift detector is a **detector, not a fixer**. It finds drift. The human or their AI agent decides what to do about it. Perhaps the spec is wrong. Perhaps the code is wrong. Perhaps both need updating. The drift detector does not know — it only knows they are out of sync.

This mirrors how code linters operate. A linter reports "unused variable on line 42." It does not delete the variable — perhaps you need it. It tells you there is a problem and you decide the fix.

### Input

The drift detector needs two things: a **baseline** (what the codebase looked like before the change) and the **changes** (what is different now). How these are provided — as git refs, a diff file, a list of changed files, or CI environment variables — is an implementation choice.

If the detector cannot determine the baseline or the changes, it MUST fail explicitly rather than guess.

The drift detector is unopinionated about branching strategy, version control workflow, or CI platform. It evaluates what changed — it does not prescribe how teams work.

### Discovery

The drift detector discovers relevant ADRs and specs by reading the indexes. It matches changed files against descriptions and tags to identify which documents may be affected by the change. Relevant tests are found through cross-referencing conventions described in the spec.

This is why well-written descriptions and meaningful tags matter — they enable precise discovery without loading every document into context.

### Evaluation

For each relevant artifact, the detector evaluates:

- Does this code change alter behavior described in a spec? If so, was the spec updated in the same change?
- Does this code change contradict a decision documented in an ADR?
- Does this spec change describe behavior not covered by tests?
- Does this test change verify behavior not described in a spec?
- Do navigation aids (entry files, READMEs, indexes) still accurately reflect the current artifacts and project structure?

### Output

The drift detector SHOULD produce linter-style output. Each finding includes:

1. **The file and lines that triggered the finding** — the code, spec, or test that appears out of sync.
2. **The affected artifact** — which ADR, spec, or test may need updating, with a direct reference.
3. **Justification** — why the detector believes drift has occurred, citing specific passages from the affected artifact.
4. **A lightweight suggestion** — the direction of the fix, not the exact text to write.

The detector MUST exit with code `0` when no drift is found and code `1` when drift is detected. Implementations MAY use additional non-zero exit codes to distinguish infrastructure errors from detected drift.

### Implementation

The framework does not prescribe a specific implementation. Teams MAY implement drift detection as an AI agent, a script, a dedicated tool, or any mechanism that satisfies the requirements above.

An LLM-based implementation is RECOMMENDED for its ability to distinguish behavioral changes from structural refactoring — a nuance that deterministic tools struggle with. A minimal implementation can be as simple as an AI agent with a prompt:

> You are a drift detector for a codebase that follows Anchored Development. Read `docs/specs/SPEC-000-anchored-development.md` to understand the framework. Read the spec and ADR indexes. Review the provided diff. Identify any ADRs, specs, tests, or navigation aids that may need updating because of these changes, and any code that has drifted from what the specs describe.

Any implementation that reliably detects drift across all four artifact types and navigation aids, and produces actionable output, satisfies the framework's requirements.

## Cross-Referencing

Cross-references between artifact types MUST be convention-based, not inventory-based.

### Why Not Inventories?

Listing exact file paths, function names, and line numbers in specs creates a maintenance trap. File paths change. Functions get renamed. Line numbers shift with every edit. Explicit inventories go stale faster than the specs they are meant to connect.

### Convention-Based Cross-Referencing

Specs reference test locations using conventions — directory paths and naming patterns rather than exact files:

> Tests for worker lifecycle behavior live in `internal/worker/` and follow the naming pattern `TestWorkerLifecycle_*`. Key behaviors verified: registration, heartbeat timeout, job assignment, graceful shutdown.

This gives drift detection enough to work with — where to look, what to look for, and what behaviors should be covered — without creating a list that needs updating every time a test file is renamed.

Cross-references between specs and ADRs are simpler — reference by document number (e.g., "See ADR-003"). Convention-based patterns are primarily needed for referencing test locations, where the connection is less obvious.

### Bidirectional Conventions

Tests MAY include convention markers (such as comments or naming patterns) that link back to the spec they verify. This enables drift detection to find relevant tests without searching the entire codebase. The specific marker format is a team implementation choice.

## The Shared Language

### Spec as the Universal Reference

Specs are written in natural language. They are readable by every stakeholder — product managers, architects, engineers, QA, and AI agents. This is deliberate.

The spec bridges the gap between "what the business needs" and "what the code does." When a product manager asks "what happens when a user cancels mid-checkout?", the answer is in the billing spec. Not in the code, which the PM cannot read. Not in a ticket from last quarter, which is stale. Not in someone's memory, which is unreliable. In the spec — which is current, accessible, and trustworthy because it is verified.

### Product Team Integration

PRDs, user stories, and product briefs are communication kickoffs. They convey intent: what the user needs, why it matters, what success looks like.

The baton pass:

1. **Product describes intent** — PRD, user story, conversation, whatever format works for the team.
2. **Architect or engineer proposes a spec update** that captures the behavioral change implied by the intent.
3. **Product reviews the spec update.** They can read it — it is natural language. "Does this capture what I meant?"
4. **Spec is committed.** Implementation proceeds. The spec may evolve during implementation as edge cases emerge.
5. **Product reads the spec at any time** to understand current system behavior.

The PRD is a communication kickoff device. It initiates the conversation. The spec captures the durable behavioral outcome. Once intent is absorbed into the spec, the PRD has served its purpose.

This makes product teams *more* involved in the ongoing system, not less. Instead of "write PRD, hand off, never see it again," it is "write PRD, review spec update, verify intent was captured, use the spec as your ongoing reference for how the system actually works."

## Adopting Anchored Development

### Required

Every repository practicing Anchored Development MUST have:

| What | Details |
| ---- | ------- |
| **This framework spec** | `docs/specs/SPEC-000-anchored-development.md`. The complete, versioned framework. |
| **Spec index** | `docs/specs/INDEX.md`. Auto-generated from spec frontmatter. |
| **ADR index** | `docs/decisions/INDEX.md`. Auto-generated from ADR frontmatter. |
| **Entry file** | A reference in the team's entry files pointing to the framework spec, artifact directories, and tooling locations. See the Navigation Aids section. |
| **Drift detection** | An enforcement mechanism that runs on every change and exits non-zero when drift is detected. See the Drift Detection section for requirements. |
| **Domain specs** | Behavioral specs (`SPEC-NNN-*.md`) written as the system's domains and behavioral expectations emerge. Core to the four-artifact feedback loop. |
| **ADRs** | Architecture Decision Records (`ADR-NNN-*.md`) written when decisions involve significant trade-offs. Core to the four-artifact feedback loop. |

### Recommended

The framework recommends but does not require:

| What | Details |
| ---- | ------- |
| **LLM-based drift detection** | For nuanced distinction between behavioral and structural changes. |
| **ADR creation skill or tooling** | Streamlines ADR writing, index generation, and validation. |
| **Spec creation skill or tooling** | Streamlines spec writing, index generation, and cross-reference scaffolding. |

The framework scales with the project. A solo developer starts with a git hook and zero specs and ADRs, adding them as domains and decisions emerge. An enterprise team might run CI pipelines and maintain dozens of specs and ADRs. The requirements are the same.

### Mapping Your Existing Documentation

Most teams already have documentation that fits the four artifact types — it just goes by different names. Adopting Anchored Development does not mean starting over. It means recognizing what you already have and ensuring it has enforcement.

| What you might have | What it is in Anchored Development |
| --- | --- |
| Architectural overview | A spec — it describes what the system does |
| Runbook or playbook | A spec — it describes expected operational behavior |
| Onboarding guide | A navigation aid — it routes new team members to artifacts |
| README | A navigation aid — it is the entry point for humans |
| AGENTS.md or CLAUDE.md | A navigation aid — it is the entry point for AI agents |
| ERD or diagram in a document | Part of the artifact it lives in |
| Generated diagrams | Self-enforcing if generated from code or schema |

If a document contains behavioral or architectural information that does not exist in any spec or ADR, that information should be extracted into the appropriate artifact type. The document itself can remain as a navigation aid — routing readers to where the authoritative information now lives.

### The Self-Proving Property

Teams that need to customize or deviate from this framework use the framework itself to document the deviation:

1. Create an **ADR** explaining why the deviation is necessary.
2. Create a **SPEC** documenting the custom convention.
3. Update **tests** and drift detection to enforce the custom convention.

The framework does not need to anticipate every customization. It provides the tools for teams to customize intentionally and traceably — through the very mechanisms it defines.

## Inspirations and References

Anchored Development draws on ideas from:

- **Birgitta Böckeler** — [Spec-Driven Development: Tools](https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html). The spec-anchored concept, the critique of spec sprawl, and the observation that SDD tools often offer one opinionated workflow unsuitable for real-world variety.

- **Drew Breunig** — [The SDD Triangle](https://www.dbreunig.com/2026/03/04/the-spec-driven-development-triangle.html). The insight that spec, tests, and code form an interconnected system where each informs the others, and the observation that drift occurs when the flow is treated as one-directional.

## Glossary

**Artifact** — A persistent file in the repository that describes or verifies the system. Code, tests, specs, and ADRs are the four artifact types. Navigation aids are not artifacts — they route readers to artifacts.

**Drift** — When an artifact or navigation aid no longer accurately reflects the current state of the system. The spec says X, the code does Y.

**Domain** — A cohesive area of system behavior. Domains persist across features. Examples: job lifecycle, authentication, billing, file transfer.

**Enforcement mode** — How an artifact or navigation aid resists drift. Three modes: self-enforcing, verified, and unverified.

**Entry file** — A navigation aid that humans or AI agents encounter first when entering the repository. READMEs serve humans. AI context files (AGENTS.md, CLAUDE.md, .cursorrules) serve AI agents.

**Living document** — A document that is edited in place when reality changes. Git provides the history. The document always describes how the system works right now.

**Markdown monster** — The anti-pattern of accumulating unverified documentation that drifts from reality and becomes actively harmful. Named for the tendency of spec-driven tools to generate excessive markdown files that nobody maintains.

**Navigation aid** — A repository document that routes readers to artifacts rather than containing authoritative system information. Always downstream — changes to artifacts may require updating navigation aids, but not the reverse. Examples: indexes, entry files, changelogs, onboarding guides.

**Self-enforcing** — An artifact whose consumer is also its executor. Drift is structurally impossible. Examples: protobuf definitions, database migrations, type definitions.

**Unverified** — An artifact or navigation aid with no automated drift detection. It will lie to you.

**Verified** — An artifact or navigation aid that is not self-enforcing but is checked by tests, CI, or other automated mechanisms. Drift is caught when it occurs.
