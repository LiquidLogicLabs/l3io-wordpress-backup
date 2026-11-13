"""Owner decision 88 — which gzip level wrote an Archive's inner payload.

A gzip stream does not record its level (the header's XFL byte only says
"fastest" or "best" for levels 1 and 9). The level is found by re-deflating
the decompressed payload with stdlib :mod:`zlib` at each level, with the
parameters :class:`gzip.GzipFile` and ``tarfile``'s ``w|gz`` both use, and
comparing the deflate bodies byte for byte. Deflate is deterministic for a
given input and level, whatever the write boundaries, so the match is exact.
A small or very regular input can deflate identically at neighbouring levels,
so the result is a set; tests use inputs where the levels they compare differ.
"""

from __future__ import annotations

import gzip
import tarfile
import zlib
from pathlib import Path

__all__ = ["deflate_body", "matching_levels", "payload_stream", "xfl"]

_FNAME = 0x08
_FEXTRA = 0x04
_FCOMMENT = 0x10
_FHCRC = 0x02


def payload_stream(archive: Path) -> bytes:
    """The outer tar's second member: the payload as written (gzip or cipher)."""
    with tarfile.open(archive, mode="r:*") as outer:
        members = outer.getmembers()
        handle = outer.extractfile(members[1])
        assert handle is not None
        return handle.read()


def deflate_body(stream: bytes) -> bytes:
    """``stream``'s deflate data: the RFC 1952 header and trailer removed."""
    assert stream[:2] == b"\x1f\x8b", "not a gzip stream"
    flags = stream[3]
    offset = 10
    if flags & _FEXTRA:
        offset += 2 + int.from_bytes(stream[offset : offset + 2], "little")
    for flag in (_FNAME, _FCOMMENT):
        if flags & flag:
            offset = stream.index(b"\x00", offset) + 1
    if flags & _FHCRC:
        offset += 2
    return stream[offset:-8]


def xfl(stream: bytes) -> int:
    """The header's XFL byte: 2 for level 9, 4 for level 1, else 0."""
    return stream[8]


def matching_levels(stream: bytes) -> frozenset[int]:
    """Every level from 0 to 9 whose deflate of the content equals ``stream``'s."""
    raw = gzip.decompress(stream)
    body = deflate_body(stream)
    found: set[int] = set()
    for level in range(10):
        deflater = zlib.compressobj(
            level, zlib.DEFLATED, -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0
        )
        if deflater.compress(raw) + deflater.flush() == body:
            found.add(level)
    return frozenset(found)
