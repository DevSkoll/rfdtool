"""Tests for rfd.ihx (Intel HEX parser)."""
from __future__ import annotations

import textwrap

import pytest

from rfd.ihx import HexImage, IHXError, parse, parse_file


def _cksum(rec_hex: str) -> str:
    """Given the body 'LLAAAATTDD...DD' return the two-hex-digit checksum byte."""
    b = bytes.fromhex(rec_hex)
    return f"{(-sum(b)) & 0xFF:02X}"


def _record(byte_count: int, address: int, record_type: int, data: bytes) -> str:
    body = f"{byte_count:02X}{address:04X}{record_type:02X}{data.hex().upper()}"
    return f":{body}{_cksum(body)}"


def _data_record(address: int, data: bytes) -> str:
    return _record(len(data), address & 0xFFFF, 0x00, data)


def _eof() -> str:
    return _record(0, 0, 0x01, b"")


# ------------------------------------------------------------- adjacency / merging

def test_adjacent_records_merge_into_one_chunk() -> None:
    text = "\n".join([
        _data_record(0x0000, bytes(range(8))),
        _data_record(0x0008, bytes(range(8, 16))),
        _eof(),
    ])
    image = parse(text)
    assert len(image.chunks) == 1
    addr, data = image.chunks[0]
    assert addr == 0x0000
    assert data == bytes(range(16))
    assert image.total_bytes() == 16
    assert image.min_address == 0x0000
    assert image.max_address == 0x0010


# ------------------------------------------------------------- gaps / to_flat

def test_non_adjacent_chunks_and_to_flat_default_fill() -> None:
    text = "\n".join([
        _data_record(0x0000, b"\xAA\xAA\xAA\xAA"),
        _data_record(0x0008, b"\xBB\xBB\xBB\xBB"),
        _eof(),
    ])
    image = parse(text)
    assert len(image.chunks) == 2
    assert image.total_bytes() == 8
    flat = image.to_flat()
    assert flat == b"\xAA\xAA\xAA\xAA\xFF\xFF\xFF\xFF\xBB\xBB\xBB\xBB"


def test_to_flat_custom_fill_byte() -> None:
    text = "\n".join([
        _data_record(0x0000, b"\x01\x02"),
        _data_record(0x0006, b"\x03\x04"),
        _eof(),
    ])
    image = parse(text)
    flat = image.to_flat(fill=0x00)
    assert flat == b"\x01\x02\x00\x00\x00\x00\x03\x04"


def test_to_flat_empty_image_returns_empty_bytes() -> None:
    image = parse(_eof())
    assert image.chunks == []
    assert image.to_flat() == b""
    assert image.min_address == 0
    assert image.max_address == 0
    assert image.total_bytes() == 0


# ------------------------------------------------------------- extended addressing

def test_extended_linear_addressing() -> None:
    ext = _record(2, 0x0000, 0x04, b"\x00\x01")
    text = "\n".join([
        ext,
        _data_record(0x1234, b"\xDE\xAD\xBE\xEF"),
        _eof(),
    ])
    image = parse(text)
    assert len(image.chunks) == 1
    addr, data = image.chunks[0]
    assert addr == 0x00011234
    assert data == b"\xDE\xAD\xBE\xEF"
    assert image.min_address == 0x00011234
    assert image.max_address == 0x00011238


def test_extended_segment_addressing() -> None:
    ext = _record(2, 0x0000, 0x02, b"\x10\x00")
    text = "\n".join([
        ext,
        _data_record(0x0000, b"\xCA\xFE"),
        _eof(),
    ])
    image = parse(text)
    addr, data = image.chunks[0]
    assert addr == 0x10000
    assert data == b"\xCA\xFE"


def test_segment_resets_on_new_segment_record() -> None:
    text = "\n".join([
        _record(2, 0x0000, 0x02, b"\x10\x00"),
        _data_record(0x0000, b"\x11"),
        _record(2, 0x0000, 0x02, b"\x20\x00"),
        _data_record(0x0000, b"\x22"),
        _eof(),
    ])
    image = parse(text)
    addrs = [c[0] for c in image.chunks]
    assert 0x10000 in addrs
    assert 0x20000 in addrs


def test_linear_record_overrides_prior_segment_base() -> None:
    text = "\n".join([
        _record(2, 0x0000, 0x02, b"\x10\x00"),
        _record(2, 0x0000, 0x04, b"\x00\x02"),
        _data_record(0x0050, b"\x01\x02"),
        _eof(),
    ])
    image = parse(text)
    addr, _ = image.chunks[0]
    assert addr == 0x00020050


# ------------------------------------------------------------- start_address

def test_start_address_from_type_03_record() -> None:
    rec03 = _record(4, 0x0000, 0x03, b"\x12\x34\x56\x78")
    text = "\n".join([rec03, _eof()])
    image = parse(text)
    assert image.start_address == (0x1234 << 16) | 0x5678


def test_start_address_from_type_05_record() -> None:
    rec05 = _record(4, 0x0000, 0x05, b"\x08\x00\x00\x00")
    text = "\n".join([rec05, _eof()])
    image = parse(text)
    assert image.start_address == 0x08000000


def test_start_address_last_one_wins() -> None:
    rec03 = _record(4, 0x0000, 0x03, b"\x00\x00\x11\x11")
    rec05 = _record(4, 0x0000, 0x05, b"\x00\x00\x22\x22")
    text = "\n".join([rec03, rec05, _eof()])
    image = parse(text)
    assert image.start_address == 0x00002222


def test_start_address_none_when_absent() -> None:
    image = parse("\n".join([_data_record(0, b"\x01"), _eof()]))
    assert image.start_address is None


# ------------------------------------------------------------- error handling

def test_bad_checksum_raises_with_line_number() -> None:
    good = _data_record(0x0000, b"\xAA")
    bad = good[:-2] + "00"
    text = "\n".join([
        _data_record(0x0010, b"\x55"),
        bad,
        _eof(),
    ])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    msg = str(exc_info.value)
    assert "line 2" in msg
    assert "checksum" in msg.lower()


def test_missing_eof_raises() -> None:
    text = _data_record(0x0000, b"\x01\x02")
    with pytest.raises(IHXError, match="EOF"):
        parse(text)


def test_bad_nibble_raises_with_line_number() -> None:
    text = "\n".join([":01000000ZZ" + "FF", _eof()])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    assert "line 1" in str(exc_info.value)


def test_wrong_length_raises_with_line_number() -> None:
    body = "0500000001020304"
    bad = f":{body}{_cksum(body)}"
    text = "\n".join([bad, _eof()])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    msg = str(exc_info.value)
    assert "line 1" in msg


def test_odd_hex_digits_raises() -> None:
    text = "\n".join([":0100000001A", _eof()])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    assert "line 1" in str(exc_info.value)


def test_overlapping_records_raise() -> None:
    text = "\n".join([
        _data_record(0x0000, b"\xAA\xBB\xCC\xDD"),
        _data_record(0x0002, b"\x11\x22"),
        _eof(),
    ])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    msg = str(exc_info.value)
    assert "line 2" in msg
    assert "overlap" in msg.lower()


def test_unknown_record_type_raises() -> None:
    body = "00000007"
    bad = f":{body}{_cksum(body)}"
    text = "\n".join([bad, _eof()])
    with pytest.raises(IHXError) as exc_info:
        parse(text)
    assert "line 1" in str(exc_info.value)


def test_record_after_eof_raises() -> None:
    text = "\n".join([
        _eof(),
        _data_record(0x0000, b"\x01"),
    ])
    with pytest.raises(IHXError):
        parse(text)


# ------------------------------------------------------------- whitespace / file

def test_whitespace_blank_lines_and_crlf() -> None:
    lines = [
        "",
        "   " + _data_record(0x0000, b"\x01\x02") + "   ",
        "",
        "\t" + _data_record(0x0002, b"\x03\x04"),
        _eof(),
        "",
    ]
    text = "\r\n".join(lines)
    image = parse(text)
    assert len(image.chunks) == 1
    assert image.chunks[0] == (0x0000, b"\x01\x02\x03\x04")


def test_non_colon_lines_are_ignored() -> None:
    text = "\n".join([
        "; this is a comment",
        "# another comment",
        _data_record(0x0000, b"\xAB"),
        _eof(),
    ])
    image = parse(text)
    assert image.chunks == [(0x0000, b"\xAB")]


def test_parse_file_round_trip(tmp_path) -> None:
    text = "\n".join([
        _data_record(0x0000, b"\xDE\xAD"),
        _data_record(0x0002, b"\xBE\xEF"),
        _eof(),
    ])
    path = tmp_path / "firmware.ihx"
    path.write_text(text)
    image = parse_file(path)
    assert image.chunks == [(0x0000, b"\xDE\xAD\xBE\xEF")]
    assert image.total_bytes() == 4
    assert image.max_address == 4


def test_parse_file_accepts_str_path(tmp_path) -> None:
    text = "\n".join([_data_record(0x10, b"\x42"), _eof()])
    path = tmp_path / "f.hex"
    path.write_text(text)
    image = parse_file(str(path))
    assert image.chunks == [(0x10, b"\x42")]


# ------------------------------------------------------------- HexImage typing

def test_hexImage_is_frozen() -> None:
    image = parse(_eof())
    with pytest.raises(Exception):
        image.start_address = 0  # type: ignore[misc]


def test_chunks_sorted_by_address() -> None:
    text = "\n".join([
        _data_record(0x0100, b"\x01"),
        _data_record(0x0000, b"\x02"),
        _data_record(0x0200, b"\x03"),
        _eof(),
    ])
    image = parse(text)
    addrs = [c[0] for c in image.chunks]
    assert addrs == sorted(addrs)
