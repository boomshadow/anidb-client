"""Tests for the AniDB field bitmask encoding.

The UDP API selects which fields a reply should contain with a hex bitmask, one
bit per field, most-significant bit first. Getting this wrong does not produce an
error -- it produces a reply whose columns are silently shifted, which then lands
in the cache as plausible-looking wrong data. These tests pin the encoding.
"""

import pytest

from anidb_client.mapper import (
    _blacklist,
    _getBitChain,
    _getCodes,
    anime_map_a,
    file_map_a,
    file_map_f,
)

ALL_MAPS = pytest.mark.parametrize(
    "attrmap",
    [anime_map_a, file_map_f, file_map_a],
    ids=["anime_map_a", "file_map_f", "file_map_a"],
)


def _selectable(attrmap):
    """Field names in a map that are actually requestable."""
    return [f for f in attrmap if f not in _blacklist]


@ALL_MAPS
def test_bitchain_length_is_one_hex_digit_per_four_fields(attrmap):
    """Every mask is fixed-width and zero-padded.

    The width matters: the server reads the mask positionally, so a mask that
    lost its leading zeros would decode as an entirely different field set.
    """
    expected_length = int(len(attrmap) / 4)
    assert len(_getBitChain(attrmap, [])) == expected_length
    assert len(_getBitChain(attrmap, _selectable(attrmap))) == expected_length


@ALL_MAPS
def test_round_trip_returns_the_requested_fields(attrmap):
    wanted = _selectable(attrmap)
    assert set(_getCodes(attrmap, _getBitChain(attrmap, wanted))) == set(wanted)


@ALL_MAPS
def test_empty_selection_produces_an_all_zero_mask(attrmap):
    chain = _getBitChain(attrmap, [])
    assert set(chain) == {"0"}
    assert _getCodes(attrmap, chain) == []


@ALL_MAPS
def test_blacklisted_fields_are_never_requested(attrmap):
    """'unused', 'retired', 'reserved' and 'not_implemented' are padding.

    They exist to hold bit positions the API defines but we cannot use. Asking
    for them would set a bit the server may answer with an extra column.
    """
    chain = _getBitChain(attrmap, list(attrmap))
    assert not set(_getCodes(attrmap, chain)) & set(_blacklist)


@ALL_MAPS
def test_each_field_round_trips_on_its_own(attrmap):
    """One field at a time, which is where zero-padding is load-bearing.

    Selecting a field late in the map yields a mask with many leading zeros. The
    implementation builds it with hex(), strips the '0x' prefix with lstrip('0x')
    -- which strips any leading '0' and 'x' characters, not just the prefix --
    and then re-pads to the fixed width. This test covers every position, so if
    that compensation is ever disturbed it fails here rather than in production.
    """
    for field in _selectable(attrmap):
        chain = _getBitChain(attrmap, [field])
        assert len(chain) == int(len(attrmap) / 4)
        assert set(_getCodes(attrmap, chain)) == {field}


@ALL_MAPS
def test_masks_are_lowercase_hex(attrmap):
    chain = _getBitChain(attrmap, _selectable(attrmap))
    assert all(c in "0123456789abcdef" for c in chain), chain


@ALL_MAPS
def test_selecting_more_fields_never_clears_a_bit(attrmap):
    """Selection is monotonic: adding a field only ever adds a column."""
    fields = _selectable(attrmap)
    first, second = fields[0], fields[-1]
    both = set(_getCodes(attrmap, _getBitChain(attrmap, [first, second])))
    assert set(_getCodes(attrmap, _getBitChain(attrmap, [first]))) <= both
    assert set(_getCodes(attrmap, _getBitChain(attrmap, [second]))) <= both


@ALL_MAPS
def test_field_positions_are_stable_against_the_wire_format(attrmap):
    """The most significant bit is the first field in the map.

    This is the one property the API contract actually rests on, and the reason
    the map lists must never be reordered.
    """
    first_selectable = _selectable(attrmap)[0]
    index = attrmap.index(first_selectable)
    chain = _getBitChain(attrmap, [first_selectable])
    bit_position = len(attrmap) - index - 1
    assert int(chain, 16) == 1 << bit_position


@ALL_MAPS
def test_selectable_field_names_are_unique(attrmap):
    """No requestable field name occupies two bit positions.

    _getBitChain tests membership by name, so a name appearing twice would set
    both bits from a single request and the reply would carry a duplicate column
    that _getCodes could not tell apart. The round-trip tests above are only
    well-defined because this holds.
    """
    selectable = _selectable(attrmap)
    assert len(selectable) == len(set(selectable))


@ALL_MAPS
def test_map_length_is_a_whole_number_of_hex_digits(attrmap):
    """Masks are padded to len(attrmap) / 4 using integer division.

    A map whose length is not a multiple of four would silently lose the leading
    digit's worth of bits when padded, so the maps must stay 4-aligned as fields
    are added.
    """
    assert len(attrmap) % 4 == 0
