---
name: publish
description: 'Cut a release of anidb-client: choose the version, write the release notes, tag, wait for CI to publish to PyPI, then create the GitLab and GitHub releases. Accepts freeform steering ("make it a pre-release", "use 0.2.0"). Rules live in docs/specs/SPEC-009-release-and-publication.md; the decision to orchestrate locally rather than in CI is ADR-004.'
---

# Publish Skill

Cut a release. The user types `/publish`, optionally with instructions, and gets three
links back: PyPI, the GitLab release, the GitHub release.

**Everything except the PyPI upload happens on this machine**, using the user's already
authenticated `glab` and `gh`. CI owns exactly one thing — verifying the tag and
uploading to PyPI over OIDC — so there is no stored credential anywhere in the pipeline.
ADR-004 records why.

## Arguments

Freeform. Read them and act; they are instructions, not a grammar.

| The user says | Means |
|---|---|
| `/publish` | Derive the version from the commits |
| `/publish rc` | Same, but as a release candidate |
| `/publish 0.2.0` | Use exactly this version |
| `/publish the next one is a pre-release, use 0.0.1-rc7` | Use `v0.0.1-rc.7` — normalise their spelling to the tag grammar and say that you did |
| `/publish this is a breaking change` | Major bump, regardless of what the commits imply |

When an instruction and the derived answer disagree, **the instruction wins** — but say
what you derived and why you are overriding it, before asking for approval.

## Preflight — every one of these is a hard stop

Do not proceed past a failure. Report it and stop; do not attempt a repair.

1. **On `main`, clean, and current.** `git status --porcelain` empty, `git rev-parse --abbrev-ref HEAD` is `main`, and `git fetch origin main` leaves nothing to pull. A release cut from a stale or dirty tree publishes something nobody reviewed.
2. **`main`'s newest *push* pipeline is green.** Filter by source — `glab api "projects/:id/pipelines?ref=main&source=push&per_page=1"`. Not simply the newest pipeline: `main`'s newest is frequently the daily security schedule, which runs three scanners and skips the entire test suite, so reading it as green clears preflight on a branch whose tests are failing. A red `main` means the thing about to be published is already known broken.
3. **`glab auth status` and `gh auth status` both succeed.** Finding out at step 5 means the tag already exists and the release is half-made.
4. **The version is not already released.** No tag of that name, locally or on the remote. Tags are permanent; see the fix-forward rule in `AGENTS.md`.

## Step 1 — decide the version

Run `task publish:next-version`. It prints the SemVer the commits imply.

**Before the first release it fails** — there is no base tag to bump from. That is
expected, not a defect. Ask the user for the number.

For a pre-release, form it from that answer: `v0.1.0` becomes `v0.1.0-rc.1`. If the
newest tag is already a pre-release of the same version, increment it instead —
`v0.1.0-rc.1` becomes `v0.1.0-rc.2`. Never renumber a pre-release backwards.

Validate the result before going any further:

```bash
task publish:check-tag -- <tag>
```

If it refuses, the message names the spelling that would have worked. Use that.

## Step 2 — write the notes and get approval

```bash
task publish:notes -- <tag>
```

That is the generated half: every commit since the last **final** release, grouped. A
pre-release is not a boundary, so a candidate's list is cumulative — `rc.3` repeats
everything `rc.1` and `rc.2` listed and adds to it. Expect that; it is not a bug, and the
paragraph is the right place to tell a tester what is new *since the previous candidate*
if that is worth saying.

**Write the paragraph that goes above it** — two to four sentences saying what this release is
*about*, in the user's register, not a restatement of the list. If the release is
thematically incoherent, say so plainly rather than inventing a theme.

Watch for entries whose commit subject misdescribes them — an internal change filed
under "Added" because it was typed `feat:`. The list cannot be edited, but the paragraph
can put it in context. Mention any you notice when you present it.

Show the user the version, the full note, and what will happen. **Wait for approval.**
Everything before this point is reversible; nothing after it is.

## Step 3 — bump, commit, tag, push

```bash
# 1. Set __version__ in src/anidb_client/__init__.py to the tag without its `v`.
# 2. Commit. The subject must be a conventional commit or the main pipeline's
#    validate:commit-messages job will reject it.
git commit -am "chore: release <tag>"

# 3. Verify BEFORE anything leaves this machine. The bump is the only commit that
#    changes the declared version and nothing else, so it is a shape no merge request
#    ever exercises and the first place it can break. The commit is still local here:
#    a failure means amend or reset, which costs nothing. The same failure after the
#    next line leaves main red and needs its own merge request to clear.
task check

# 4. Push to main. This is the one place anything is pushed to main directly,
#    and it is deliberate -- see ADR-004.
git push origin main

# 5. Annotated tag, on that commit, pushed on its own.
git tag -a <tag> -m "<tag>"
git push origin <tag>
```

Order matters. The tag must point at the commit carrying the matching `__version__`, or
the publish gate will reject it — correctly.

## Step 4 — watch the tag pipeline

Find the pipeline **for the tag ref**, and poll it **by id**:

```bash
glab api "projects/:id/pipelines?ref=<tag>&per_page=1"
glab api "projects/:id/pipelines/<id>"
```

Do not use `glab ci status --branch`. It returns the newest pipeline for a ref, which
can be a different pipeline than the one you mean — a branch pipeline instead of a merge
request's, for instance. Those contain different jobs, so it will cheerfully report
success for a pipeline that never ran the gate you are waiting on.

Wait for a terminal state. **Green means `publish:pypi` succeeded and the package is
live.** Red means nothing was published.

## Step 5 — create both releases

Only after the pipeline is green. PyPI is the irreversible step; the release pages are
the announcement, and announcing a package that does not exist is the one ordering
mistake worth avoiding.

Fetch the built artifacts so the GitHub release carries the same files that were
published, rather than a local rebuild:

```bash
glab ci artifact <tag> build:dist
```

Then create both, with the **same body** — the paragraph followed by the generated list:

```bash
glab release create <tag> --notes-file <file>
gh release create <tag> --repo boomshadow/anidb-client \
  --notes-file <file> --verify-tag [--prerelease] dist/*
```

`--prerelease` when the version carries an `-alpha.N` / `-beta.N` / `-rc.N` segment.

`--verify-tag` matters: the tag reaches GitHub through the push mirror, which is
asynchronous. By the time the pipeline has finished it will normally have arrived. If it
has not, wait and retry rather than letting `gh` create a tag of its own — that would
leave GitHub with a lightweight tag where GitLab has an annotated one.

## Step 6 — report

Give the user three links: the PyPI project page, the GitLab release, the GitHub
release. Say plainly whether it was a pre-release, and if so, remind them that an
ordinary `pip install` will not pick it up.

## When something fails

**The pipeline goes red after the tag is pushed.** Do not move or delete the tag. Report
what failed and stop. The repair is the next version — `AGENTS.md` and SPEC-009 both
state this, and protected tags enforce it: GitLab will refuse the update outright.

**A release page fails to create.** PyPI already has the package, so the release is real
and only the announcement is missing. Re-running that step alone is safe; both commands
are idempotent enough to retry against an existing tag.

**Never** offer to force a tag, re-upload a version, or work around the gate. The gate
refusing is the system working.
