"""FR-10 — the committed v0 fixtures are what their generator writes.

``make_v0_fixtures.build`` is run into ``tmp_path`` and every Archive it writes
must be byte-identical to the committed file of the same name; a generator
change that was not re-run, or a hand-edited fixture, fails. The scope is the
committed tree, not the generator's list: every Archive directly in ``v0/`` and
in ``v0/malformed/`` (selected by ``tests/archive_fixtures.py::archive_files``)
except the released fixture must have been generated, so a fixture committed
without its generator fails too. The released fixture is never regenerated:
its committed SHA-256, in ``released-0.2.21-all.provenance.json``, must match it.
"""

from __future__ import annotations

import hashlib
import json
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


def _differs(built: Path, committed: Path) -> list[str]:
    return sorted(
        p.relative_to(built).as_posix()
        for p in built.rglob("*")
        if p.is_file()
        and p.read_bytes() != (committed / p.relative_to(built)).read_bytes()
    )


def test_every_committed_fixture_is_regenerated_byte_for_byte(tmp_path: Path) -> None:
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
