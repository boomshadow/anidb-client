"""Tests for file hashing and the filename-parsing regexes.

AniDB identifies a file by its ed2k hash, so the hash is the primary key for
every lookup: getting it wrong means every file misses, and every miss is a
request against the flood limit.

The interesting part of ed2k is its chunking. A file is split into 9,728,000-byte
chunks; a single-chunk file hashes as plain MD4 of its contents, while a
multi-chunk file hashes as MD4 over the concatenated MD4 digests of its chunks.
The boundary between those two rules is where the bugs live, so it is tested from
both sides.
"""

import pytest
from Crypto.Hash import MD4

from anidb_client.fileinfo import (
    _calculate_ed2khash,
    ep_nr_re,
    get_file_hash,
    get_file_stats,
    multiep_re,
    partfile_re,
    specials_re,
)

CHUNK = 9_728_000


def _md4(data: bytes) -> str:
    return MD4.new(data).hexdigest()


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


class TestEd2kHashing:
    def test_single_chunk_file_hashes_as_plain_md4(self, tmp_path):
        data = b"anidb test payload"
        assert get_file_hash(str(_write(tmp_path, "a.mkv", data))) == _md4(data)

    def test_a_file_of_exactly_one_chunk_uses_the_single_chunk_rule(self, tmp_path):
        """Exactly 9,728,000 bytes reads as one chunk, then EOF.

        This is the boundary: one byte more and the two-level rule applies
        instead. Both sides are pinned so a change to the read loop cannot
        silently move it.
        """
        data = b"\x00" * CHUNK
        assert get_file_hash(str(_write(tmp_path, "exact.mkv", data))) == _md4(data)

    def test_a_file_over_one_chunk_hashes_the_concatenated_chunk_digests(self, tmp_path):
        data = b"\x00" * (CHUNK + 1)
        expected = _md4(MD4.new(data[:CHUNK]).digest() + MD4.new(data[CHUNK:]).digest())
        assert get_file_hash(str(_write(tmp_path, "over.mkv", data))) == expected

    def test_the_two_rules_disagree_at_the_boundary(self, tmp_path):
        """A one-byte difference must change the hash scheme, not just the input."""
        exact = get_file_hash(str(_write(tmp_path, "exact.mkv", b"\x00" * CHUNK)))
        over = get_file_hash(str(_write(tmp_path, "over.mkv", b"\x00" * (CHUNK + 1))))
        assert exact != over

    def test_hashing_is_deterministic(self, tmp_path):
        p = str(_write(tmp_path, "a.mkv", b"repeatable"))
        assert get_file_hash(p) == get_file_hash(p)

    def test_content_change_changes_the_hash(self, tmp_path):
        a = get_file_hash(str(_write(tmp_path, "a.mkv", b"one")))
        b = get_file_hash(str(_write(tmp_path, "b.mkv", b"two")))
        assert a != b

    def test_hash_is_lowercase_hex_of_the_expected_width(self, tmp_path):
        digest = get_file_hash(str(_write(tmp_path, "a.mkv", b"x")))
        assert len(digest) == 32
        assert all(c in "0123456789abcdef" for c in digest)

    def test_empty_file_hashes_as_md4_of_nothing(self, tmp_path):
        """ed2k defines the empty file's hash as MD4 over no bytes.

        Previously this raised IndexError: the chunk generator yields nothing,
        so the multi-chunk branch indexed hashes[0] on an empty list.
        """
        assert get_file_hash(str(_write(tmp_path, "empty.mkv", b""))) == _md4(b"")

    def test_hashing_accepts_any_binary_stream(self, tmp_path):
        """_calculate_ed2khash takes a file object, not only a path."""
        p = _write(tmp_path, "a.mkv", b"stream")
        with open(p, "rb") as fh:
            assert _calculate_ed2khash(fh) == _md4(b"stream")


class TestFileStats:
    def test_returns_mtime_and_size(self, tmp_path):
        p = _write(tmp_path, "a.mkv", b"12345")
        mtime, size = get_file_stats(str(p))
        assert size == 5
        assert mtime.year >= 2000


class TestEpisodeNumberRegexes:
    """The patterns that guess an episode number from a filename.

    These are ordered: the list contains a `None` sentinel separating confident
    patterns from fallbacks that must not run for single-episode anime. Order and
    the sentinel are both part of the contract.
    """

    def test_the_fallback_sentinel_is_present_exactly_once(self):
        assert ep_nr_re.count(None) == 1

    def test_confident_patterns_come_before_the_sentinel(self):
        boundary = ep_nr_re.index(None)
        assert boundary > 0
        assert all(r is not None for r in ep_nr_re[:boundary])
        assert all(r is not None for r in ep_nr_re[boundary + 1 :])

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("/show/foo.s01.e05.mkv", "05"),
            ("/show/foo.S01E05.mkv", "05"),
            ("/show/foo.ep05.mkv", "05"),
            ("/show/foo.EP_05.mkv", "05"),
            ("/show/foo.1x09.mkv", "09"),
            ("/show/foo - 05.mkv", "05"),
        ],
    )
    def test_confident_patterns_extract_the_episode_number(self, filename, expected):
        boundary = ep_nr_re.index(None)
        for regex in ep_nr_re[:boundary]:
            match = regex.search(filename)
            if match:
                assert match.group(2) == expected
                return
        pytest.fail(f"no confident pattern matched {filename!r}")

    @pytest.mark.parametrize(
        ("filename", "marker"),
        [
            ("/show/foo.special.02.mkv", "s"),
            ("/show/foo.NCOP01.mkv", "o"),
            ("/show/foo.NCED01.mkv", "e"),
            ("/show/foo.trailer01.mkv", "t"),
        ],
    )
    def test_special_type_patterns_capture_their_type_marker(self, filename, marker):
        """Group 1 carries the AniDB special prefix (S/O/E/T/C/P)."""
        for regex in ep_nr_re:
            if regex is None:
                continue
            match = regex.search(filename)
            if match and match.group(1):
                assert match.group(1).lower() == marker
                return
        pytest.fail(f"no pattern captured a type marker for {filename!r}")

    def test_the_last_pattern_matches_any_bare_number(self):
        """The final fallback exists so that a plain number is still a guess."""
        match = ep_nr_re[-1].search("/show/foo 7.mkv")
        assert match and match.group(2) == "7"


class TestOtherRegexes:
    @pytest.mark.parametrize(
        "filename",
        ["/show/foo.part1.mkv", "/show/foo part 2.mkv", "/show/foo.part_iv.mkv"],
    )
    def test_partfile_regex_recognises_split_files(self, filename):
        assert partfile_re.search(filename)

    def test_partfile_regex_ignores_ordinary_names(self):
        assert not partfile_re.search("/show/foo - 05.mkv")

    def test_partfile_regex_does_not_match_the_pt_abbreviation(self):
        """Documents current behaviour, which looks like it was not intended.

        The pattern is `(p)(?:ar)t`, so the literal "part" is required. Writing
        it as `(p)(?:ar)?t` -- almost certainly what was meant, given the capture
        group isolates the "p" -- would additionally match the common "pt2"
        abbreviation.

        Left as-is deliberately: loosening it changes which files are treated as
        partial episodes, and that reclassifies files in existing collections.
        That is a behavioural decision, not a tidy-up, so it is recorded here
        rather than quietly changed.
        """
        assert not partfile_re.search("/show/foo-pt2.mkv")

    @pytest.mark.parametrize(
        ("epno", "kind", "number"),
        [("S1", "S", "1"), ("C03", "C", "03"), ("T2", "T", "2"), ("O1", "O", "1")],
    )
    def test_specials_regex_splits_prefix_from_number(self, epno, kind, number):
        match = specials_re.match(epno)
        assert match and match.group(1).upper() == kind and match.group(2) == number

    def test_specials_regex_rejects_a_plain_episode_number(self):
        assert not specials_re.match("12")

    def test_multiep_regex_finds_every_number(self):
        assert multiep_re.findall("01-03") == ["01", "03"]
