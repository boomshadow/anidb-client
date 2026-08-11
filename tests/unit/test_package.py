"""Smoke tests for the package itself.

These assert the things that must hold before any other test is meaningful: the
package imports, its version is discoverable, and the identity it will send to
AniDB is what we think it is.
"""

import importlib.metadata

import anidb_client


def test_package_imports_and_exposes_version():
    assert isinstance(anidb_client.__version__, str)
    assert anidb_client.__version__.count(".") == 2, "expected a semantic version"


def test_installed_metadata_version_matches_package():
    """The build backend reads __version__ from the package.

    If these two ever disagree, the wheel on PyPI is labelled with a version that
    the code inside it does not report.
    """
    assert importlib.metadata.version("anidb-client") == anidb_client.__version__


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
