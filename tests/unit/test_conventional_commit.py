"""The conventional-commit gate, and its agreement with the release-note routing.

Two files have to stay in step: `scripts/conventional_commit.py` decides which commit
types are allowed, and `cliff.toml` decides which heading each type appears under in a
release note. Neither notices when they disagree -- a type nothing routes lands
silently in the catch-all, and a route nothing can produce is simply never taken. The
cross-check at the bottom is what makes that disagreement fail. See SPEC-009 and
ADR-003.
"""

import io
import re
import tomllib
from pathlib import Path

import pytest

from scripts.conventional_commit import (
    ALLOWED_TYPES,
    NotConventional,
    check,
    main,
    review,
)

CLIFF_CONFIG = Path(__file__).resolve().parents[2] / "cliff.toml"


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add a thing",
        "fix: stop the thing breaking",
        "feat(cache): add a thing",
        "feat!: change a thing incompatibly",
        "feat(cache)!: change a thing incompatibly",
        "docs: explain the thing",
        "chore: bump a pin",
        # The Keep a Changelog types the Angular convention has no word for.
        "remove: drop the deprecated thing",
        "deprecate: mark the thing for removal",
        "security: close the hole",
    ],
)
def test_well_formed_subjects_pass(subject: str) -> None:
    check(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "add a thing",  # no type
        "feat add a thing",  # no colon
        "feat:add a thing",  # no space after the colon
        "feat: ",  # no description
        "Feat: add a thing",  # types are lowercase
        "feet: add a thing",  # a typo the routing table would send to the catch-all
        "wip: add a thing",  # not in the vocabulary
        "",
        "   ",
    ],
)
def test_malformed_subjects_are_refused(subject: str) -> None:
    with pytest.raises(NotConventional):
        check(subject)


def test_a_typo_in_the_type_is_named_as_such():
    """`feet:` parses as a conventional commit -- it is the vocabulary that rejects it.

    Worth distinguishing: the grammar is fine, so a message about the grammar would
    send the reader looking in the wrong place.
    """
    with pytest.raises(NotConventional, match="not one this project routes"):
        check("feet: add a thing")


def test_the_refusal_offers_a_corrected_line():
    """This fires when someone is trying to merge. The useful answer is what to type."""
    with pytest.raises(NotConventional, match="fix: add a thing"):
        check("add a thing")


@pytest.mark.parametrize("prefix", ["Draft: ", "WIP: ", "draft: "])
def test_gitlab_draft_prefixes_are_ignored(prefix: str) -> None:
    """GitLab strips these before the title becomes a subject, so they are not a fault."""
    check(f"{prefix}feat: add a thing")


@pytest.mark.parametrize(
    "subject",
    [
        "Merge branch 'topic' into 'main'",
        "Merge remote-tracking branch 'origin/main'",
        "Merge tag 'v1.0.0'",
    ],
)
def test_merge_subjects_are_skipped_rather_than_refused(subject: str) -> None:
    """Nobody authored these and they never reach a release note."""
    reviewed = review([subject])

    assert reviewed.complaints == []
    assert reviewed.checked == 0
    assert reviewed.skipped == 1


def test_every_bad_subject_in_a_batch_is_reported():
    """A push of several commits should not need one CI run per mistake."""
    assert len(review(["feat: fine", "broken one", "also broken"]).complaints) == 2


def test_the_count_reports_what_was_validated_not_what_was_read():
    """A run that validated nothing must not read like a run that validated everything.

    Counting lines received rather than subjects checked would let a push of nothing
    but merge commits announce that it had inspected them all -- the same shape of
    false success as a publish that uploads no artifacts.
    """
    reviewed = review(["Merge branch 'x' into 'main'", "", "feat: a real one"])

    assert reviewed.checked == 1
    assert reviewed.skipped == 2


def test_a_push_of_only_merges_checks_nothing_and_says_so(capsys):
    """Legitimate, so it passes -- but the summary must not overstate what it did."""
    stream = io.StringIO("Merge branch 'a' into 'main'\nMerge branch 'b' into 'main'\n")

    assert main(["--stdin"], stdin=stream) == 0
    out = capsys.readouterr().out
    assert "0 subject(s) checked" in out
    assert "2 skipped" in out


def test_cli_accepts_a_title(capsys):
    assert main(["--title", "feat: add a thing"]) == 0


def test_cli_rejects_a_bad_title(capsys):
    assert main(["--title", "add a thing"]) == 1
    assert "SPEC-009" in capsys.readouterr().err


def test_cli_reads_subjects_from_stdin(capsys):
    stream = io.StringIO("feat: fine\nMerge branch 'x' into 'main'\nbroken\n")

    assert main(["--stdin"], stdin=stream) == 1
    assert "broken" in capsys.readouterr().err


# --- The cross-check -------------------------------------------------------------


def _commit_parsers() -> list[dict[str, object]]:
    with CLIFF_CONFIG.open("rb") as handle:
        config = tomllib.load(handle)
    parsers: list[dict[str, object]] = config["git"]["commit_parsers"]
    return parsers


def _heading_for(subject: str) -> str | None:
    """Return the heading `cliff.toml` would file `subject` under, or None if skipped.

    Mirrors git-cliff's own rule: parsers are tried in order and the first match wins.
    """
    for parser in _commit_parsers():
        pattern = parser.get("message")
        if not isinstance(pattern, str) or not re.search(pattern, subject):
            continue
        if parser.get("skip"):
            return None
        group = parser.get("group")
        return group if isinstance(group, str) else None
    return None


def _catch_all_heading() -> str | None:
    return _heading_for("this is not a conventional commit at all")


def test_the_routing_table_has_a_catch_all():
    """Without one, an unparsable commit would vanish from the release notes entirely."""
    assert _catch_all_heading() is not None


@pytest.mark.parametrize("commit_type", sorted(ALLOWED_TYPES))
def test_every_allowed_type_routes_to_a_heading_of_its_own(commit_type: str) -> None:
    """An allowed type that falls through to the catch-all is a silent misfiling.

    Nothing else would catch it: the commit is well-formed, CI passes, and the entry
    just quietly appears under the wrong heading months later.
    """
    heading = _heading_for(f"{commit_type}: a description")

    assert heading is not None, f"{commit_type!r} is skipped by cliff.toml"
    assert heading != _catch_all_heading(), f"{commit_type!r} falls through to the catch-all in cliff.toml"


def test_no_heading_is_unreachable():
    """The other direction: a route no allowed type can produce is dead configuration.

    Usually it means a type was renamed in one file and not the other.
    """
    reachable = {_heading_for(f"{commit_type}: a description") for commit_type in ALLOWED_TYPES}
    reachable.add(_catch_all_heading())

    declared = {
        parser["group"]
        for parser in _commit_parsers()
        if not parser.get("skip") and isinstance(parser.get("group"), str)
    }

    assert declared <= reachable, f"unreachable cliff.toml headings: {sorted(declared - reachable)}"
