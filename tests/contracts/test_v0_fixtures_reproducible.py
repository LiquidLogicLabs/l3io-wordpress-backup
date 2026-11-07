"""FR-10 — the committed v0 fixtures are what their generator writes.

``make_v0_fixtures.build`` is run into ``tmp_path`` and every Archive it writes
must match the committed file of the same name; a generator change that was
not re-run, or a hand-edited fixture, fails. Archives are compared by their
**decompressed** bytes, not their ``.tar.gz`` bytes: the compressed stream
depends on the zlib build, which differs across CI Pythons and OSes
(BL-E004-021). A file whose gzip stream is incomplete by design
(``malformed/truncated.tar.gz``) matches when both sides fail to decompress
and one's decompressible prefix is a prefix of the other's. The scope is the
committed tree, not the generator's list: every Archive directly in ``v0/`` and
in ``v0/malformed/`` (selected by ``tests/archive_fixtures.py::archive_files``)
except the released fixture must have been generated, so a fixture committed
without its generator fails too. The released fixture is never regenerated:
its committed SHA-256, in ``released-0.2.21-all.provenance.json``, must match it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from pathlib import Path

from tests.archive_fixtures import archive_files
from tests.fixtures.archives.v0 import make_v0_fixtures

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V0 = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v0"
RELEASED = V0 / "released-0.2.21-all.tar.gz"
PROVENANCE = V0 / "released-0.2.21-all.provenance.json"


def _committed(root: Path = V0) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in (*archive_files(root), *archive_files(root / "malformed"))
        if p.name != RELEASED.name
    )


def _content(path: Path) -> tuple[bool, bytes]:
    """``(complete, decompressed bytes)``; a broken stream gives its prefix."""
    data = path.read_bytes()
    try:
        return True, gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        partial = zlib.decompressobj(wbits=31)
        try:
            return False, partial.decompress(data)
        except zlib.error:
            return False, b""


def _same(built: Path, committed: Path) -> bool:
    (whole_a, a), (whole_b, b) = _content(built), _content(committed)
    if whole_a or whole_b:
        return whole_a == whole_b and a == b
    return a.startswith(b) or b.startswith(a)


def _differs(built: Path, committed: Path) -> list[str]:
    return sorted(
        p.relative_to(built).as_posix()
        for p in built.rglob("*")
        if p.is_file() and not _same(p, committed / p.relative_to(built))
    )


def test_every_committed_fixture_is_regenerated_to_the_same_content(
    tmp_path: Path,
) -> None:
    built = make_v0_fixtures.build(tmp_path)
    names = sorted(p.relative_to(tmp_path).as_posix() for p in built)
    committed = _committed()
    assert committed, f"no committed v0 fixtures under {V0}"
    assert names == committed, (
        f"committed without the generator: {sorted(set(committed) - set(names))}; "
        f"generated but not committed: {sorted(set(names) - set(committed))}"
    )
    assert _differs(tmp_path, V0) == []


def test_the_guard_sees_a_hand_edited_fixture(tmp_path: Path) -> None:
    make_v0_fixtures.build(tmp_path / "a")
    make_v0_fixtures.build(tmp_path / "b")
    edited = tmp_path / "b" / "malformed" / "hardlink.tar.gz"
    data = bytearray(edited.read_bytes())
    data[-1] ^= 0x01
    edited.write_bytes(data)
    assert _differs(tmp_path / "a", tmp_path / "b") == ["malformed/hardlink.tar.gz"]


def test_a_recompressed_fixture_is_the_same_and_an_edited_member_is_not(
    tmp_path: Path,
) -> None:
    """Another zlib's bytes for the same tar pass; a changed member fails."""
    make_v0_fixtures.build(tmp_path / "a")
    make_v0_fixtures.build(tmp_path / "b")
    name = "synthetic-files-only.tar.gz"
    tar = gzip.decompress((tmp_path / "a" / name).read_bytes())
    recompressed = gzip.compress(tar, compresslevel=1, mtime=0)
    assert recompressed != (tmp_path / "a" / name).read_bytes()
    (tmp_path / "b" / name).write_bytes(recompressed)
    assert _differs(tmp_path / "a", tmp_path / "b") == []
    edited = bytearray(tar)
    edited[600] ^= 0x01  # inside the first member's data block
    (tmp_path / "b" / name).write_bytes(gzip.compress(bytes(edited), mtime=0))
    assert _differs(tmp_path / "a", tmp_path / "b") == [name]


def test_the_released_fixture_matches_its_provenance() -> None:
    assert PROVENANCE.is_file(), f"{PROVENANCE.name} must be committed"
    assert RELEASED.is_file(), f"{RELEASED.name} must be committed"
    record = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    for key in ("command", "tool_version", "python_version", "date", "sha256"):
        assert record.get(key), f"provenance is missing {key!r}"
    assert record["tool_version"] == "0.2.21"
    assert record["sha256"] == hashlib.sha256(RELEASED.read_bytes()).hexdigest()


def test_the_scope_sees_a_fixture_committed_without_the_generator(
    tmp_path: Path,
) -> None:
    built = make_v0_fixtures.build(tmp_path)
    (tmp_path / "malformed" / "hand-made.tar.gz").write_bytes(b"")
    (tmp_path / "released-0.2.21-all.provenance.json").write_text("{}", "utf-8")
    names = sorted(p.relative_to(tmp_path).as_posix() for p in built)
    assert sorted(set(_committed(tmp_path)) - set(names)) == [
        "malformed/hand-made.tar.gz"
    ]
