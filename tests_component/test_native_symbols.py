# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the actual C ELF parser against relocated and malformed inputs."""

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def symbol_reader(tmp_path_factory):
    if configured := os.environ.get("P3_SYMBOL_TEST_HELPER"):
        return configured
    if not (compiler := shutil.which("cc")):
        pytest.skip("Native parser tests require cc or P3_SYMBOL_TEST_HELPER")
    binary = tmp_path_factory.mktemp("native-symbols") / "reader"
    source = Path(__file__).with_name("native_symbols_harness.c")
    subprocess.run([compiler, "-O2", str(source), "-o", str(binary)], check=True)
    return str(binary)


def make_elf(*, shift=0, origin=0, duplicate=False, missing=False, wrong_scope=False):
    data = bytearray(4096)
    strings = bytearray(b"\0")
    symbols = [bytes(16)]

    def add(name, value=0, size=0, kind=4, section=0xFFF1):
        offset = len(strings)
        strings.extend(name.encode() + b"\0")
        symbols.append(struct.pack("<IIIBBH", offset, value, size, kind, 0, section))

    # Same anonymous name in another file must not be mistaken for readiness.
    add("unrelated.c")
    add("__compound_literal.0", origin + 0x3000, 52, 1, 3)
    add("ha_ir.c")
    ready = origin + 0x3100 + shift
    add("__compound_literal.0", ready, 52, 1, 3)
    add("ha_ir_master.c")
    match = origin + 0x3200 + shift
    add("g_match_info", match, 4, 1, 3)
    if duplicate:
        add("g_match_info", match + 4, 4, 1, 3)
    add("wrong.c" if wrong_scope else "ha_ir_power.c")
    add("count.123", origin + 0x3300, 1, 1, 3)
    samples, pending, batches = [origin + x + shift for x in (0x3400, 0x3600, 0x3800)]
    add("count.555", samples, 4, 1, 3)
    if not missing:
        add("sum_power.444", pending, 4, 1, 3)
    add("time_cout.222", batches, 4, 1, 3)
    ident = b"\x7fELF\x01\x01\x01" + bytes(9)
    struct.pack_into(
        "<16sHHIIIIIHHHHHH", data, 0, ident, 3, 8, 1, 0, 52, 128, 0, 52, 32, 1, 40, 4, 0
    )
    struct.pack_into("<8I", data, 52, 1, 0, origin, 0, 128, 128, 5, 4096)
    table = b"".join(symbols)
    struct.pack_into(
        "<10I", data, 168, 0, 2, 0, 0, 512, len(table), 2, len(symbols), 4, 16
    )
    struct.pack_into("<10I", data, 208, 0, 3, 0, 0, 1536, len(strings), 0, 0, 1, 0)
    struct.pack_into("<10I", data, 248, 0, 8, 3, origin + 0x3000, 0, 4096, 0, 0, 4, 0)
    data[512 : 512 + len(table)] = table
    data[1536 : 1536 + len(strings)] = strings
    return data, [origin, ready, match, batches, samples, pending]


@pytest.mark.parametrize(("shift", "origin"), [(0, 0), (64, 0), (128, 4096)])
def test_symbols_follow_elf_values_and_file_scope(
    symbol_reader, tmp_path, shift, origin
):
    data, expected = make_elf(shift=shift, origin=origin)
    source = tmp_path / "library.so"
    source.write_bytes(data)
    result = subprocess.run(
        [symbol_reader, str(source)], capture_output=True, text=True, check=True
    )
    assert [int(value) for value in result.stdout.split()] == expected


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "missing",
        "wrong_scope",
        "stripped",
        "truncated",
        "wrong_endian",
        "wrong_machine",
        "bad_strings",
        "bad_symbol_range",
        "bad_section_offset",
    ],
)
def test_invalid_elf_fails_without_address_fallback(symbol_reader, tmp_path, fault):
    data, _ = make_elf(
        **{fault: True} if fault in {"duplicate", "missing", "wrong_scope"} else {}
    )
    if fault == "stripped":
        struct.pack_into("<I", data, 172, 11)  # .dynsym alone is insufficient.
    elif fault == "truncated":
        data = data[:48]
    elif fault == "wrong_endian":
        data[5] = 2
    elif fault == "wrong_machine":
        struct.pack_into("<H", data, 18, 3)
    elif fault == "bad_strings":
        struct.pack_into("<I", data, 228, 1)
    elif fault == "bad_symbol_range":
        struct.pack_into("<I", data, 260, 0x5000)
    elif fault == "bad_section_offset":
        struct.pack_into("<I", data, 32, 0xFFFFFFF0)
    source = tmp_path / "library.so"
    source.write_bytes(data)
    result = subprocess.run(
        [symbol_reader, str(source)], capture_output=True, check=False
    )
    assert result.returncode == 2
    assert result.stdout == b""
