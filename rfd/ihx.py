"""Pure-Python Intel HEX (.ihx / .hex) parser.

Produces a flat byte image suitable for XMODEM upload to the SiK bootloader on
8051-based RFD900 radios. Stdlib only; no I/O beyond :func:`parse_file`.

Intel HEX record format (per the Intel Hexadecimal Object File Format
Specification, Rev A): each line is ``:LLAAAATT[DD...]CC`` in ASCII hex, where
``LL`` is the data byte count, ``AAAA`` the 16-bit address (interpretation
depends on record type), ``TT`` the record type (00..05), ``DD`` data bytes,
and ``CC`` the two's-complement checksum of all preceding bytes on the line.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class IHXError(ValueError):
    """Intel HEX parse error. The error message includes the source line number."""


@dataclass(frozen=True)
class HexImage:
    chunks: list[tuple[int, bytes]]
    start_address: int | None
    min_address: int
    max_address: int

    def to_flat(self, *, fill: int = 0xFF) -> bytes:
        if not self.chunks:
            return b""
        if not 0 <= fill <= 0xFF:
            raise ValueError(f"fill byte out of range: {fill}")
        size = self.max_address - self.min_address
        out = bytearray([fill]) * size
        base = self.min_address
        for addr, data in self.chunks:
            offset = addr - base
            out[offset:offset + len(data)] = data
        return bytes(out)

    def total_bytes(self) -> int:
        return sum(len(data) for _addr, data in self.chunks)


_RECORD_DATA = 0x00
_RECORD_EOF = 0x01
_RECORD_EXT_SEG = 0x02
_RECORD_START_SEG = 0x03
_RECORD_EXT_LIN = 0x04
_RECORD_START_LIN = 0x05


def _decode_line(line: str, line_no: int) -> tuple[int, int, int, bytes]:
    body = line.strip()
    if not body.startswith(":"):
        raise IHXError(f"line {line_no}: missing ':' start code")
    body = body[1:]
    if len(body) < 10:
        raise IHXError(f"line {line_no}: record too short ({len(body)} hex chars)")
    if len(body) % 2 != 0:
        raise IHXError(f"line {line_no}: odd number of hex digits")
    try:
        raw = bytes.fromhex(body)
    except ValueError as exc:
        raise IHXError(f"line {line_no}: bad hex digit ({exc})") from exc

    byte_count = raw[0]
    address = (raw[1] << 8) | raw[2]
    record_type = raw[3]
    expected_len = 5 + byte_count
    if len(raw) != expected_len:
        raise IHXError(
            f"line {line_no}: length mismatch (header says {byte_count} data bytes, "
            f"line carries {len(raw) - 5})"
        )

    if (sum(raw) & 0xFF) != 0:
        raise IHXError(f"line {line_no}: bad checksum")

    data = bytes(raw[4:4 + byte_count])
    return record_type, address, byte_count, data


def parse(text: str) -> HexImage:
    chunks: list[tuple[int, bytearray]] = []
    base: int = 0
    start_address: int | None = None
    saw_eof = False

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if not stripped.startswith(":"):
            continue

        if saw_eof:
            raise IHXError(f"line {line_no}: record after EOF")

        record_type, address, byte_count, data = _decode_line(raw_line, line_no)

        if record_type == _RECORD_DATA:
            absolute = base + address
            _insert_data(chunks, absolute, data, line_no)

        elif record_type == _RECORD_EOF:
            if byte_count != 0:
                raise IHXError(f"line {line_no}: EOF record must have zero data bytes")
            saw_eof = True

        elif record_type == _RECORD_EXT_SEG:
            if byte_count != 2:
                raise IHXError(
                    f"line {line_no}: extended segment record must carry 2 data bytes"
                )
            value = (data[0] << 8) | data[1]
            base = (value * 16) & 0xFFFFFFFF

        elif record_type == _RECORD_START_SEG:
            if byte_count != 4:
                raise IHXError(
                    f"line {line_no}: start segment record must carry 4 data bytes"
                )
            cs = (data[0] << 8) | data[1]
            ip = (data[2] << 8) | data[3]
            start_address = (cs << 16) | ip

        elif record_type == _RECORD_EXT_LIN:
            if byte_count != 2:
                raise IHXError(
                    f"line {line_no}: extended linear record must carry 2 data bytes"
                )
            value = (data[0] << 8) | data[1]
            base = (value << 16) & 0xFFFFFFFF

        elif record_type == _RECORD_START_LIN:
            if byte_count != 4:
                raise IHXError(
                    f"line {line_no}: start linear record must carry 4 data bytes"
                )
            start_address = int.from_bytes(data, "big")

        else:
            raise IHXError(f"line {line_no}: unknown record type 0x{record_type:02X}")

    if not saw_eof:
        raise IHXError("missing EOF record (type 01)")

    finalised: list[tuple[int, bytes]] = [(addr, bytes(buf)) for addr, buf in chunks]
    finalised.sort(key=lambda c: c[0])

    if finalised:
        min_addr = finalised[0][0]
        last_addr, last_data = finalised[-1]
        max_addr = last_addr + len(last_data)
    else:
        min_addr = 0
        max_addr = 0

    return HexImage(
        chunks=finalised,
        start_address=start_address,
        min_address=min_addr,
        max_address=max_addr,
    )


def parse_file(path: str | os.PathLike[str]) -> HexImage:
    return parse(Path(path).read_text())


def _insert_data(
    chunks: list[tuple[int, bytearray]],
    address: int,
    data: bytes,
    line_no: int,
) -> None:
    if not data:
        return

    end = address + len(data)
    for idx, (chunk_addr, chunk_buf) in enumerate(chunks):
        chunk_end = chunk_addr + len(chunk_buf)
        if address >= chunk_end or end <= chunk_addr:
            continue
        raise IHXError(
            f"line {line_no}: data overlaps existing chunk "
            f"(0x{address:X}..0x{end:X} vs 0x{chunk_addr:X}..0x{chunk_end:X})"
        )

    for idx, (chunk_addr, chunk_buf) in enumerate(chunks):
        chunk_end = chunk_addr + len(chunk_buf)
        if address == chunk_end:
            chunk_buf.extend(data)
            _maybe_merge_forward(chunks, idx)
            return
        if end == chunk_addr:
            merged = bytearray(data)
            merged.extend(chunk_buf)
            chunks[idx] = (address, merged)
            _maybe_merge_backward(chunks, idx)
            return

    chunks.append((address, bytearray(data)))


def _maybe_merge_forward(chunks: list[tuple[int, bytearray]], idx: int) -> None:
    addr, buf = chunks[idx]
    end = addr + len(buf)
    for j, (other_addr, other_buf) in enumerate(chunks):
        if j == idx:
            continue
        if other_addr == end:
            buf.extend(other_buf)
            chunks.pop(j)
            return


def _maybe_merge_backward(chunks: list[tuple[int, bytearray]], idx: int) -> None:
    addr, buf = chunks[idx]
    for j, (other_addr, other_buf) in enumerate(chunks):
        if j == idx:
            continue
        other_end = other_addr + len(other_buf)
        if other_end == addr:
            other_buf.extend(buf)
            chunks[j] = (other_addr, other_buf)
            chunks.pop(idx)
            return
