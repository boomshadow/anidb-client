"""Equality and per-instance state on File objects.

Two small things that were wrong in ways that had not yet cost anything, and
were cheap to make right while the file was open:

- `__eq__` fell off the end and returned None for two files that were simply
  different, rather than False;
- `_multiep` defaulted to a list declared on the class, which every File in the
  process therefore shared.
"""

import pytest

from anidb_client.animeobjs import File
from tests import factories


@pytest.fixture
def two_files(anidb, session):
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    session.add(factories.make_episode(aid=6187, eid=96462, epno="6"))
    session.add(factories.make_file(aid=6187, eid=96461, fid=12345))
    session.add(factories.make_file(aid=6187, eid=96462, fid=12346))
    session.commit()
    return anidb.File(fid=12345), anidb.File(fid=12346)


class TestEquality:
    def test_two_different_files_compare_false(self, two_files):
        """Was None. Falsy, so `==` looked right, but it is not a bool and
        anything inspecting the result itself saw None."""
        first, second = two_files

        assert (first == second) is False

    def test_two_different_files_are_unequal(self, two_files):
        first, second = two_files

        assert first != second

    def test_a_file_equals_itself_by_fid(self, two_files, anidb):
        first, _second = two_files

        assert first == anidb.File(fid=12345)

    def test_comparing_with_another_type_is_not_implemented(self, two_files):
        """NotImplemented lets Python fall back to the other operand, and then to
        identity. It must not be confused with the new False."""
        first, _second = two_files

        assert first.__eq__("not a file") is NotImplemented
        assert first != "not a file"


class TestMultiepDefault:
    def test_the_class_default_is_not_a_shared_mutable(self):
        """A list here is one object shared by every File in the process.

        Nothing appends to it today -- every write rebinds -- which is why this
        has never bitten, and is exactly when it is cheap to remove.
        """
        assert File._multiep is None

    def test_each_file_gets_its_own_episode_list(self, two_files):
        first, second = two_files
        first._multiep = ["5", "6", "7"]

        assert second._multiep is None, "one file's episodes must not appear on another"

    def test_an_unset_multiep_still_resolves_to_this_file_s_episode(self, two_files):
        """None has to read as "not worked out yet", exactly as [] did."""
        first, _second = two_files

        assert first.multiep == ["5"]
