"""Equality, containment and per-instance state on File objects.

This is the file SPEC-001 names as covering equality and containment.

The equality and `_multiep` cases were small things wrong in ways that had not
yet cost anything, and were cheap to make right while the file was open:

- `__eq__` fell off the end and returned None for two files that were simply
  different, rather than False;
- `_multiep` defaulted to a list declared on the class, which every File in the
  process therefore shared.

Containment is the one that had started costing something -- see TestContainment.
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


class TestContainment:
    """`in` coerces whatever `__contains__` returns straight to a bool.

    There is no reflected form to fall back to, the way there is for `__eq__`, so
    `NotImplemented` is not a legal answer here. Returning it read as True on
    3.13 -- `NotImplemented` is truthy -- and raises `TypeError` on 3.14, which
    made `anything in anime` an error. Both are wrong: the answer is False.
    """

    def test_a_files_own_episode_is_in_it(self, two_files):
        first, _second = two_files

        assert first.episode in first

    def test_another_files_episode_is_not_in_it(self, two_files):
        first, second = two_files

        assert second.episode not in first

    def test_an_unrelated_type_is_not_in_a_file(self, two_files):
        first, _second = two_files

        assert ("not an episode" in first) is False

    def test_an_episode_of_the_anime_is_in_it(self, two_files, anidb):
        first, _second = two_files

        assert first.episode in anidb.Anime(6187)

    def test_an_unrelated_type_is_not_in_an_anime(self, anidb):
        assert (12345 in anidb.Anime(6187)) is False


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


@pytest.fixture
def ranged_file(anidb, session):
    """A file whose episode number is itself a range, as AniDB records them.

    A single file covering episodes 5 to 7 gets one episode row with epno "5-7";
    `multiep` is what expands that back into the individual numbers.
    """
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96470, epno="5-7"))
    session.add(factories.make_file(aid=6187, eid=96470, fid=12350))
    session.commit()
    return anidb.File(fid=12350)


class TestARangedFilesEpisodeNumbers:
    """The multi-episode branch used to answer with a `range` of ints.

    Every other branch of `multiep` produces a list of strings, and the two are
    not interchangeable: `Episode.episode_number` is a string, `"5" in range(5, 8)`
    is False rather than an error, and the mylist commands put whatever they are
    given straight on the wire. So the one branch that exists to describe a file
    spanning several episodes was the one that answered wrongly about them.
    """

    def test_the_range_is_expanded_into_strings(self, ranged_file):
        assert ranged_file.multiep == ["5", "6", "7"]

    def test_every_branch_of_the_property_agrees_on_its_type(self, ranged_file, anidb, session):
        """A property with two return types is the defect, not the range itself."""
        session.add(factories.make_episode(aid=6187, eid=96473, epno="9"))
        session.add(factories.make_file(aid=6187, eid=96473, fid=12351))
        session.commit()
        single = anidb.File(fid=12351)

        assert {type(x) for x in ranged_file.multiep} == {str}
        assert {type(x) for x in single.multiep} == {str}

    def test_each_episode_of_the_range_is_in_the_file(self, ranged_file, anidb, session):
        """What `__contains__` exists to answer, and answered False for.

        `"6" in range(5, 8)` does not raise -- range falls back to iteration and
        no int equals a string -- so this returned False silently.
        """
        session.add(factories.make_episode(aid=6187, eid=96471, epno="6"))
        session.commit()

        assert anidb.Episode(eid=96471) in ranged_file

    def test_an_episode_outside_the_range_is_not_in_the_file(self, ranged_file, anidb, session):
        session.add(factories.make_episode(aid=6187, eid=96472, epno="9"))
        session.commit()

        assert anidb.Episode(eid=96472) not in ranged_file
