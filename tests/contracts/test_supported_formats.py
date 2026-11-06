"""ADR-0017 — ``SUPPORTED_FORMAT_VERSIONS`` is what the fixture Archives prove.

The set **equals** the ``format_version`` values read from the Manifest of every
fixture Archive under ``tests/fixtures/archives/v*/`` (globbed, asserted
non-empty; each ``v<N>`` directory holds only Archives of version ``N``, plus
the script that generated them and sidecars). Archives are selected by
``tests/archive_fixtures.py::archive_files``, the one rule. Every Archive
directly in ``v0/`` must classify as v0, and ``v0/`` must hold at least one:
v0 is accepted by shape, never by ``SUPPORTED_FORMAT_VERSIONS``, so a classifier
that accepts v0 with no committed v0 fixture fails here (E004-S01-002). The
failing set in ``v0/malformed/`` is a directory, so it is never selected.
"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

from l3io.wp.backup.adapters.tar import TarArchiveProbe
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    SUPPORTED_FORMAT_VERSIONS,
    classify,
    manifest_from_json,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.archive_fixtures import archive_files

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "archives"
V0 = FIXTURES / "v0"


def _format_version(archive: Path) -> int:
    with tarfile.open(archive, mode="r|") as outer:
        first = outer.next()
        assert first is not None
        assert first.name == MANIFEST_MEMBER, archive
        handle = outer.extractfile(first)
        assert handle is not None
        return manifest_from_json(handle.read().decode("utf-8")).format_version


def test_the_fixture_glob_is_not_empty() -> None:
    versioned = [d for d in FIXTURES.glob("v*/") if d.name != V0.name]
    assert versioned, f"no fixture directories under {FIXTURES}"
    assert all(archive_files(d) for d in versioned), "an empty fixture directory"


def test_supported_versions_equal_the_fixtures() -> None:
    found: set[int] = set()
    for directory in sorted(FIXTURES.glob("v*/")):
        if directory.name == V0.name:
            continue
        for archive in archive_files(directory):
            version = _format_version(archive)
            assert f"v{version}" == directory.name, archive
            found.add(version)
    assert found == SUPPORTED_FORMAT_VERSIONS


def test_every_v0_fixture_classifies_as_v0() -> None:
    archives = archive_files(V0)
    assert archives, f"v0 is accepted by shape but {V0} holds no fixture Archive"
    probe = TarArchiveProbe()
    for archive in archives:
        target = StorageTarget(scheme="file", path=PurePosixPath(archive))
        assert classify(probe.member_index(target)).format_version is None, archive
