"""The release-tag gate.

This is the rule that decides whether a tag is allowed to publish anything, and
what version it must publish under. It runs twice for every release -- once
before the tag is created and once before the upload -- so it is worth pinning
precisely. See SPEC-009 and ADR-002.
"""

import pytest

from scripts.release_tag import (
    InvalidReleaseTag,
    check_artifacts,
    declared_version,
    distribution_version,
    main,
)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.0.1", "0.0.1"),
        ("v1.2.3", "1.2.3"),
        ("v10.20.30", "10.20.30"),
        # SemVer spells a pre-release with a hyphen and a dot; PEP 440 spells the
        # same thing with neither. This translation is the whole reason the tag
        # grammar and the distribution version are not simply the same string.
        ("v0.0.1-rc.1", "0.0.1rc1"),
        ("v0.0.1-alpha.1", "0.0.1a1"),
        ("v0.0.1-beta.2", "0.0.1b2"),
        ("v2.0.0-rc.10", "2.0.0rc10"),
        # A zeroth pre-release is legal in both notations and should survive.
        ("v1.0.0-rc.0", "1.0.0rc0"),
    ],
)
def test_legal_tags_map_to_their_distribution_version(tag: str, expected: str) -> None:
    assert distribution_version(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "0.0.1",  # no v prefix
        "v0.0",  # not three components
        "v0.0.1.2",  # four components
        "v0.0.1rc1",  # the PEP 440 spelling, which is not what tags use
        "v0.0.1-rc1",  # missing the SemVer separator dot
        "v0.0.1-rc",  # no pre-release number
        "v0.0.1-pre.1",  # an identifier PEP 440 cannot express
        "v0.0.1-RC.1",  # pre-release identifiers are lowercase
        "v01.0.0",  # leading zeros are not SemVer
        "v0.0.1-rc.01",  # ...including inside the pre-release number
        "v0-upstream",  # the pre-existing non-release tag in this repository
        "main",
        "",
    ],
)
def test_illegal_tags_are_refused(tag: str) -> None:
    with pytest.raises(InvalidReleaseTag):
        distribution_version(tag)


def test_build_metadata_is_refused_with_its_own_explanation():
    """SemVer allows `+build.5`; PyPI refuses every version carrying a local label.

    Worth its own message rather than the generic one: the tag is well-formed
    SemVer, so "that is not a release tag" would read as a bug in the gate.
    """
    with pytest.raises(InvalidReleaseTag, match="build metadata"):
        distribution_version("v1.0.0+build.5")


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [
        ("v0.0.1-rc1", r"v0\.0\.1-rc\.1"),  # the generic refusal offers an example
        ("v1.0.0+build.5", r"v1\.0\.0"),  # the build-metadata refusal offers this very tag, trimmed
    ],
)
def test_every_refusal_names_a_spelling_that_would_have_worked(tag: str, accepted: str) -> None:
    """The gate fires months apart, at the exact moment the spelling is forgotten.

    A refusal that only says no leaves the reader guessing at the moment they can
    least afford to guess, so both refusal paths carry an accepted form -- not just
    the generic one.
    """
    with pytest.raises(InvalidReleaseTag, match=accepted):
        distribution_version(tag)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("anidb_client-0.0.1-py3-none-any.whl", "0.0.1"),
        ("anidb_client-0.0.1rc1-py3-none-any.whl", "0.0.1rc1"),
        ("anidb_client-0.0.1.tar.gz", "0.0.1"),
        ("anidb_client-0.0.1rc1.tar.gz", "0.0.1rc1"),
        # Not distributions, so they carry no version to disagree with.
        ("README.md", None),
        ("anidb_client-0.0.1.whl", None),
    ],
)
def test_version_is_read_from_the_artifact_filename(filename: str, expected: str | None) -> None:
    assert declared_version(filename) == expected


def test_artifacts_matching_the_tag_produce_no_complaints(tmp_path):
    (tmp_path / "anidb_client-0.0.1rc1-py3-none-any.whl").touch()
    (tmp_path / "anidb_client-0.0.1rc1.tar.gz").touch()

    assert check_artifacts(tmp_path, "0.0.1rc1") == []


def test_an_artifact_disagreeing_with_the_tag_is_reported(tmp_path):
    """The case this gate exists for: the tag is on a commit that declares something else."""
    (tmp_path / "anidb_client-0.0.1-py3-none-any.whl").touch()
    (tmp_path / "anidb_client-0.0.1.tar.gz").touch()

    complaints = check_artifacts(tmp_path, "0.0.2")

    assert len(complaints) == 2
    assert all("0.0.2" in complaint for complaint in complaints)


def test_an_empty_dist_directory_is_itself_a_failure(tmp_path):
    """A publish that uploads nothing must not report success."""
    (tmp_path / "not-a-distribution.txt").touch()

    assert check_artifacts(tmp_path, "0.0.1") == ["no wheel or sdist found in " + str(tmp_path)]


def test_cli_prints_the_version_for_a_legal_tag(capsys):
    assert main(["v0.0.1-rc.1"]) == 0
    assert capsys.readouterr().out.strip() == "0.0.1rc1"


def test_cli_fails_for_an_illegal_tag(capsys):
    assert main(["v0.0.1-rc1"]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_checks_artifacts_when_given_a_dist_directory(tmp_path, capsys):
    (tmp_path / "anidb_client-0.0.9-py3-none-any.whl").touch()

    assert main(["v0.0.1", "--dist", str(tmp_path)]) == 1
    assert "declares version 0.0.9" in capsys.readouterr().err


def test_cli_rejects_a_missing_dist_directory(tmp_path, capsys):
    assert main(["v0.0.1", "--dist", str(tmp_path / "absent")]) == 1
    assert "is not a directory" in capsys.readouterr().err
