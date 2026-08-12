"""Decoding the optional state bitmask on a FILE reply.

AniDB sends `state` only when the request's fmask asked for it, so the field is
genuinely optional -- but the decoder ran unconditionally. `None & 0x1` raises
TypeError, and it raises inside the callback, on the listener's response thread,
*before* the completion event at the end of the method. So the reply arrived, was
parsed successfully, and every caller waiting on that file blocked forever
regardless. That is precisely the failure SPEC-002's "a waiter is always
released" rule exists to prevent, which is why this is worth pinning.
"""

import pytest

from tests import factories
from tests.objectlayer import FakeResponse

FOUND = "220"


def _reply(**extra):
    """A minimal FILE hit, plus whatever the test wants to add."""
    dataline = {"fid": "12345", "aid": "6187", "eid": "96461", "gid": "1"}
    dataline.update(extra)
    return FakeResponse(FOUND, datalines=[dataline])


@pytest.fixture
def a_file(anidb, session):
    session.add(factories.make_anime(aid=6187))
    session.add(factories.make_episode(aid=6187, eid=96461, epno="5"))
    session.commit()
    return anidb.File(anime=6187, episode="5")


class TestOptionalStateField:
    def test_a_reply_without_state_does_not_raise(self, a_file):
        a_file._anidb_file_data_callback(_reply())

    def test_a_reply_without_state_still_releases_the_waiter(self, a_file):
        """The part that turned a decode error into a hang."""
        a_file._anidb_file_data_callback(_reply())

        assert a_file._file_updated.is_set(), "a parsed reply must always release its waiter"

    def test_a_reply_without_state_leaves_the_decoded_fields_unset(self, a_file):
        """Absent means unknown. Inventing a default would be a worse answer than
        having none, since nothing distinguishes it from a real reading."""
        a_file._anidb_file_data_callback(_reply())

        assert a_file.db_data.crc_ok is None
        assert a_file.db_data.file_version is None

    @pytest.mark.parametrize(
        ("state", "crc_ok", "version"),
        [("1", True, 1), ("2", False, 1), ("5", True, 2), ("9", True, 3)],
    )
    def test_a_reply_with_state_still_decodes_it(self, a_file, state, crc_ok, version):
        a_file._anidb_file_data_callback(_reply(state=state))

        assert a_file.db_data.crc_ok is crc_ok
        assert a_file.db_data.file_version == version
