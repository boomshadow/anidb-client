#!/usr/bin/env python3
"""Validate a release tag and report the distribution version it must produce.

Git tags in this project are SemVer; Python distributions are PEP 440. The two
agree on ordinary releases and disagree on pre-releases -- SemVer spells one
`0.1.0-rc.1`, PEP 440 spells it `0.1.0rc1` -- so the mapping lives here once
rather than being restated wherever a version is needed. See ADR-002.

The same rule is applied from both sides of the tag: `task publish:check-tag` runs it
before one is created, and the publish pipeline runs it before anything is uploaded.
A tag this rejects is a tag that must not exist.

    scripts/release_tag.py v0.1.0-rc.1              # prints 0.1.0rc1
    scripts/release_tag.py v0.1.0-rc.1 --dist dist  # also checks the built artifacts
"""

import argparse
import re
import sys
from pathlib import Path

# A release tag is `v` plus a SemVer version, with the pre-release grammar
# deliberately narrowed to the three identifiers PEP 440 can express. SemVer
# permits any dot-separated identifier; a tag spelling Python cannot represent
# would publish under a version nobody asked for, so it is refused instead.
TAG_PATTERN = re.compile(
    r"^v"
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre_word>alpha|beta|rc)\.(?P<pre_number>0|[1-9]\d*))?"
    r"$"
)

# PEP 440's spellings for the same three identifiers.
PRE_RELEASE_ABBREVIATIONS = {"alpha": "a", "beta": "b", "rc": "rc"}

WHEEL_SUFFIX = ".whl"
SDIST_SUFFIX = ".tar.gz"


class InvalidReleaseTag(ValueError):
    """A tag that must not publish anything."""


def distribution_version(tag: str) -> str:
    """Return the PEP 440 version a legal release tag must produce.

    Raises InvalidReleaseTag, carrying the spelling that would have been accepted,
    for anything that is not a release tag.
    """
    # Checked ahead of the pattern so the commonest SemVer-ism gets its own answer
    # rather than the generic one. PyPI refuses any version with a local label, so
    # a tag carrying build metadata cannot publish however well-formed it looks.
    if "+" in tag:
        without_metadata = tag.split("+", 1)[0]
        raise InvalidReleaseTag(
            f"{tag!r} carries build metadata. PyPI rejects local version labels, so a tag that publishes "
            f"must not contain a '+' segment -- use {without_metadata!r}."
        )

    match: re.Match[str] | None = TAG_PATTERN.match(tag)
    if match is None:
        raise InvalidReleaseTag(
            f"{tag!r} is not a release tag. Expected v<major>.<minor>.<patch>, optionally followed by "
            f"-alpha.N, -beta.N or -rc.N -- for example v0.0.1 or v0.0.1-rc.1."
        )

    version = f"{match.group('major')}.{match.group('minor')}.{match.group('patch')}"
    word = match.group("pre_word")
    if word is not None:
        version += f"{PRE_RELEASE_ABBREVIATIONS[word]}{int(match.group('pre_number'))}"
    return version


def declared_version(filename: str) -> str | None:
    """Return the version a distribution filename declares, or None if it declares none.

    Both filename forms put the version in the same place: the field after the
    distribution name. Anything that is not a wheel or an sdist has no version to
    read and is not this function's business.
    """
    for suffix, expected_fields in ((WHEEL_SUFFIX, 5), (SDIST_SUFFIX, 2)):
        if filename.endswith(suffix):
            fields = filename[: -len(suffix)].split("-")
            return fields[1] if len(fields) == expected_fields else None
    return None


def check_artifacts(dist_dir: Path, expected: str) -> list[str]:
    """Return one complaint per built artifact whose version is not `expected`.

    An empty directory is itself a complaint: a publish job that uploads nothing
    while reporting success is the failure this check exists to prevent.
    """
    complaints: list[str] = []
    found = False

    for path in sorted(dist_dir.iterdir()):
        version = declared_version(path.name)
        if version is None:
            continue
        found = True
        if version != expected:
            complaints.append(f"{path.name} declares version {version}, expected {expected}")

    if not found:
        complaints.append(f"no wheel or sdist found in {dist_dir}")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("tag", help="the git tag to validate, e.g. v0.0.1-rc.1")
    parser.add_argument(
        "--dist",
        type=Path,
        metavar="DIR",
        help="also verify every wheel and sdist in DIR carries the version the tag implies",
    )
    args = parser.parse_args(argv)

    try:
        version = distribution_version(args.tag)
    except InvalidReleaseTag as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.dist is not None:
        if not args.dist.is_dir():
            print(f"ERROR: {args.dist} is not a directory", file=sys.stderr)
            return 1
        complaints = check_artifacts(args.dist, version)
        for complaint in complaints:
            print(f"ERROR: {complaint}", file=sys.stderr)
        if complaints:
            print(
                f"\nThe tag {args.tag} and the built artifacts disagree. Either the tag is on the wrong "
                f"commit, or __version__ was not bumped to match it.",
                file=sys.stderr,
            )
            return 1

    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
