# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import pytest

from custom_components.aqara_p3.protocol.codebook import Codebook, NativeState
from custom_components.aqara_p3.protocol.errors import InvalidData, UnsupportedState


def test_full_state_plan_has_one_exact_command(book_bytes):
    book = Codebook.parse(book_bytes)
    state = NativeState(True, 0, 27, 0, 0)
    record = book.plan(state)
    assert record.key == "P0_M0_T27_S0_D0"
    assert book.plan(NativeState(False, 0, 27, 0, 0)).key.startswith("P1_")
    assert book.plan(NativeState(True, 3, None, 0, 0)).key == "P0_M3_S0_D0"


def test_missing_state_is_not_approximated(book_bytes):
    with pytest.raises(UnsupportedState):
        Codebook.parse(book_bytes).plan(NativeState(True, 1, 16, 0, 0))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b[:-10],
        lambda b: b.replace(b"1||3", b"1||4"),
        lambda b: b.replace(b"P1_M0_T27_S0_D0", b"P0_M0_T27_S0_D0"),
        lambda b: b.replace(b"AQID", b"!!!!", 1),
        lambda b: b.replace(b"0|2|0|0", b"0|9|0|0"),
        lambda b: b.replace(b"|3|AQID", b"|9999999|AQID", 1),
    ],
)
def test_reject_damaged_codebook(book_bytes, mutation):
    with pytest.raises(InvalidData):
        Codebook.parse(mutation(book_bytes))


def test_temperature_absent_for_fan_mode():
    with pytest.raises(UnsupportedState):
        NativeState(True, 3, 0, 0, 0)
    with pytest.raises(UnsupportedState):
        NativeState(True, 0, 27.5, 0, 0)


def test_index_does_not_claim_transmit_verified(book_bytes):
    summary = Codebook.parse(book_bytes).summary()
    assert summary["execution_verified"] is False
    assert summary["modes"]["cool"]["temperatures"] == [27]
