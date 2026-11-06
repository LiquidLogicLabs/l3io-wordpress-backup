"""RT-2 — a tar extended-header body is bounded when an untrusted Archive is read.

``tarfile`` reads a GNU long-name/long-link or pax header body whole into
memory. The bombs here declare 512 MiB but carry 128 MiB of zeros (about 130 KiB
gzipped), so the unfixed reader allocates the 128 MiB it is given and the fixed
one refuses at the header, before reading any body. What the fixed reader still
holds is tarfile's own gzip stream buffer (about 25 MiB for a 10 KiB chunk of
zeros), a constant, so the limit sits between the two.
"""

from __future__ import annotations

import gzip
import tarfile
import tracemalloc
from pathlib import Path

import pytest

from l3io.wp.backup.adapters import tar as tar_module
from l3io.wp.backup.adapters.tar import read_inner_manifest, verify_archive
from l3io.wp.backup.domain.errors import ArchiveReadError, PayloadDamagedError

_CHUNK = 1024 * 1024
DECLARED = 512 * 1024 * 1024
CARRIED = 128 * 1024 * 1024
#: Under what carrying CARRIED bytes costs, over tarfile's constant buffers.
PEAK_LIMIT = 64 * 1024 * 1024


def _bomb(path: Path, kind: bytes) -> Path:
    header = tarfile.TarInfo("././@LongLink")
    header.type = kind
    header.size = DECLARED
    with gzip.GzipFile(path, "wb", compresslevel=1) as out:
        out.write(header.tobuf(tarfile.GNU_FORMAT))
        for _ in range(CARRIED // _CHUNK):
            out.write(bytes(_CHUNK))
    return path


def _peak(action: object) -> int:
    tracemalloc.start()
    try:
        action()  # type: ignore[operator]
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
    ],
    ids=["gnu-L", "gnu-K", "pax-x", "pax-g"],
)
def test_verify_refuses_an_extended_header_bomb_with_bounded_memory(
    tmp_path: Path, kind: bytes
) -> None:
    path = _bomb(tmp_path / "bomb.tar.gz", kind)
    results: list[object] = []
    peak = _peak(lambda: results.append(verify_archive(path)))
    (result,) = results
    assert result.passed is False  # type: ignore[attr-defined]
    assert peak < PEAK_LIMIT


def test_the_inner_payload_reader_refuses_the_bomb_as_an_unreadable_archive(
    tmp_path: Path,
) -> None:
    path = _bomb(tmp_path / "payload.tar.gz", tarfile.GNUTYPE_LONGNAME)
    # Warm the lazy imports of the first call so they are not measured.
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(PayloadDamagedError):
        read_inner_manifest(empty)

    def read() -> None:
        with pytest.raises(ArchiveReadError) as caught:
            read_inner_manifest(path)
        assert str(DECLARED) not in str(caught.value)

    assert _peak(read) < PEAK_LIMIT


def test_a_legitimate_long_name_still_reads(tmp_path: Path) -> None:
    long_name = "a/" + "d" * 200 + "/" + "n" * 200 + ".txt"
    path = tmp_path / "long.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as out:
        info = tarfile.TarInfo(long_name)
        out.addfile(info)
    with tarfile.open(path, "r:gz", tarinfo=tar_module._BoundedTarInfo) as back:  # noqa: SLF001
        assert back.getnames() == [long_name]
