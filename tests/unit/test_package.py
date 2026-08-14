"""Smoke tests for the package itself.

These assert the things that must hold before any other test is meaningful: the
package imports, its version is discoverable, and the identity it will send to
AniDB is what we think it is.
"""

import importlib.metadata
import tomllib
from pathlib import Path

import anidb_client
from scripts.release_tag import distribution_version

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = "src/anidb_client/__init__.py"


def test_package_imports_and_exposes_a_taggable_version():
    """`__version__` is the release tag without its `v`, so it must be a legal one.

    Catching a malformed version here rather than at publish time means a bad bump
    fails on the merge request that introduced it, not months later on the tag.
    """
    assert isinstance(anidb_client.__version__, str)
    distribution_version(f"v{anidb_client.__version__}")


def test_installed_metadata_version_matches_package():
    """The build backend reads __version__ from the package and normalises it.

    `__version__` is SemVer and the installed metadata is PEP 440, so these agree
    exactly for an ordinary release and differ in spelling for a pre-release --
    `0.0.1-rc.1` against `0.0.1rc1`. Comparing through the same translation the
    publish gate applies checks both halves at once: that the build backend
    normalises the way the gate predicts, and that the wheel on PyPI is not
    labelled with a version the code inside it does not report.
    """
    expected = distribution_version(f"v{anidb_client.__version__}")
    assert importlib.metadata.version("anidb-client") == expected


def test_anidb_client_identity_is_independent_of_package_version():
    """The AUTH identity is a registration, not a release number.

    These were previously coupled -- the packaging computed the distribution
    version from `anidb_client_version`, so bumping the protocol registration
    silently bumped the published package. This pins them apart.
    """
    assert isinstance(anidb_client.anidb_client_name, str) and anidb_client.anidb_client_name
    assert isinstance(anidb_client.anidb_client_version, int)
    assert anidb_client.anidb_api_version == 3

    # The old packaging computed the distribution version as
    # `f"{anidb_client_version / 10:.1f}.0"`. Pin that formula as dead rather than
    # merely asserting the two values look different -- which is what this test
    # did at first, and it broke the moment the registered version became 1,
    # because "1" is a substring of "100".
    derived_the_old_way = f"{anidb_client.anidb_client_version / 10:.1f}.0"
    assert anidb_client.__version__ != derived_the_old_way


def test_the_version_file_invalidates_the_build_cache():
    """uv must rebuild this project when the declared version changes.

    This guards a line in `pyproject.toml` that looks redundant and is not. uv's default
    cache keys already cover the whole `src` directory, so naming one file inside it
    reads like belt and braces -- but a content change to a file in `src` does not
    invalidate the cached build. Measured: bump `__version__` alone, sync, and the
    installed metadata still reports the old number.

    What that costs is narrow and badly timed. The release bump is the only commit that
    changes the version file and nothing else, so a stale build shows up on the release
    itself and never on a merge request -- the one moment where a failure is expensive.

    Anyone tidying that key away as duplicated should fail here first.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    keys = config["tool"]["uv"]["cache-keys"]
    named = [entry["file"] for entry in keys if "file" in entry]

    assert VERSION_FILE in named, f"{VERSION_FILE} must be a uv cache key -- see the comment on cache-keys"
    # A key naming a path that no longer exists invalidates nothing and reports nothing.
    assert (REPO_ROOT / VERSION_FILE).is_file(), f"the cache key names {VERSION_FILE}, which does not exist"
