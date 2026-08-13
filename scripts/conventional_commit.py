#!/usr/bin/env python3
"""Validate that a commit subject or merge-request title is a conventional commit.

Two things depend on the answer, and neither notices when it is wrong. The type
selects which heading a change appears under in a release note, and a type nothing
routes lands in the catch-all -- silently, because a generator has no way to know a
subject was mistyped. This is what turns that into an error someone sees.

Where it runs is deliberate, and follows from squash-on-merge:

  --title   validates a merge-request title, which is what GitLab turns into the
            squashed commit's subject on the default branch. The branch's own
            commits are discarded by the squash, so linting them would reject work
            that never reaches history.

  (stdin)   validates commit subjects, one per line, for a push to the default
            branch. That covers both a direct push made without a merge request and
            a squash message hand-edited at merge time -- the two ways a subject
            reaches the default branch without a merge-request title ever being
            checked.

    scripts/conventional_commit.py --title "feat: add a thing"
    git log --format=%s "$RANGE" | scripts/conventional_commit.py --stdin
"""

import argparse
import re
import sys
from typing import TextIO

# The vocabulary. Every entry must be routed to a heading by `cliff.toml`, and every
# heading `cliff.toml` names must be reachable from an entry here -- a type nothing
# routes falls into the catch-all, and a route nothing can produce is dead. Neither
# is visible by reading either file alone, so a test holds the two against each other.
#
# Wider than the Angular convention, because Keep a Changelog has headings the
# convention has no type for. `remove`, `deprecate` and `security` exist to reach
# them; Conventional Commits explicitly permits types beyond feat and fix.
ALLOWED_TYPES = frozenset(
    {
        "build",
        "chore",
        "ci",
        "deprecate",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "remove",
        "revert",
        "security",
        "style",
        "test",
    }
)

# type(optional scope)optional-!: description
SUBJECT_PATTERN = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^()]+)\))?"
    r"(?P<breaking>!)?"
    r": (?P<description>.+)$"
)

# GitLab's own prefixes for a merge request that is not ready. They are removed by the
# platform before the title becomes a commit subject, so they are not the author's
# problem and must not be reported as one.
DRAFT_PREFIX_PATTERN = re.compile(r"^(?:Draft|WIP):\s*", re.IGNORECASE)

# Subjects a merge produces. They never reach a release note and are never authored,
# so holding them to the convention would fail a push for text nobody wrote.
MERGE_SUBJECT_PATTERN = re.compile(r"^Merge (?:branch|remote-tracking branch|tag) ")


class NotConventional(ValueError):
    """A subject that must not become a line of history."""


def check(subject: str) -> None:
    """Raise NotConventional unless `subject` is a well-formed conventional commit.

    The message always shows a corrected form of what was actually written, rather
    than abstract grammar: this fires at the moment someone is trying to merge, and
    the useful answer is the line they should have typed.
    """
    stripped = DRAFT_PREFIX_PATTERN.sub("", subject).strip()

    if not stripped:
        raise NotConventional("subject is empty")

    match = SUBJECT_PATTERN.match(stripped)
    if match is None:
        raise NotConventional(
            f"{stripped!r} is not a conventional commit. Expected '<type>: <description>', "
            f"optionally '<type>(<scope>): ' or '<type>!: ' for a breaking change -- "
            f"for example 'fix: {stripped[:50].lower()}'."
        )

    commit_type = match.group("type")
    if commit_type not in ALLOWED_TYPES:
        raise NotConventional(
            f"{stripped!r} uses the type {commit_type!r}, which is not one this project routes. "
            f"Use one of: {', '.join(sorted(ALLOWED_TYPES))}."
        )


def check_all(subjects: list[str]) -> list[str]:
    """Return one complaint per subject that is not a conventional commit.

    Merge subjects are skipped rather than reported.
    """
    complaints: list[str] = []
    for subject in subjects:
        if not subject.strip() or MERGE_SUBJECT_PATTERN.match(subject):
            continue
        try:
            check(subject)
        except NotConventional as error:
            complaints.append(str(error))
    return complaints


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate conventional commit subjects.")
    source = parser.add_mutually_exclusive_group(required=True)
    # nargs="+" so the subject survives an unquoted shell word split. A title contains
    # spaces and a colon, and the callers that pass one -- a CI variable, a Taskfile
    # argument -- do not all preserve quoting. Rejoining is more forgiving than being
    # right about whose quoting is at fault.
    source.add_argument(
        "--title",
        nargs="+",
        metavar="WORD",
        help="a single subject to validate, e.g. a merge-request title",
    )
    source.add_argument(
        "--stdin",
        action="store_true",
        help="read subjects from standard input, one per line",
    )
    args = parser.parse_args(argv)

    if args.title is not None:
        subjects = [" ".join(args.title)]
    else:
        stream = sys.stdin if stdin is None else stdin
        subjects = stream.readlines()

    complaints = check_all([subject.rstrip("\n") for subject in subjects])
    for complaint in complaints:
        print(f"ERROR: {complaint}", file=sys.stderr)

    if complaints:
        print(
            "\nThis subject becomes a line of the release notes -- see SPEC-009. "
            "Reword it, or amend the commit, before merging.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(subjects)} subject(s) checked, all conventional.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
