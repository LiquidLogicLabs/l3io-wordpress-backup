"""E007-S01-002 (FR-29, AD-18) — ``drive_restore`` with the config one level up.

Driven through ``tests/restore_root_rig.py`` (the real ``drive_restore``, the
real extension, a fake database): an Archive whose config was at
``<root>/../wp-config.php`` restored into a Site Root with no config.

* the journal header's ``wp_config_path`` and the survey's ``config_exists``
  name the file the cutover writes, ``<root>/../wp-config.php`` (the O2 edit
  to ``cli/restore.py``), and the plan lists the lay-down;
* killed inside the cutover (an exception no handler catches stands in for
  SIGKILL; ``tests/integration/test_config_parent_resolve.py`` kills a real
  process), ``--resolve-interrupted-run`` completes the cutover in the parent
  file and never creates ``<root>/wp-config.php``;
* a Dry Run prints the lay-down and lays nothing down.
"""

from __future__ import annotations

import dataclasses
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.config_writer import WpConfigWriter
from l3io.wp.backup.adapters.journal import FileRunJournal, journal_directory
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.restore import RelocationFlags, RestoreOutcome
from l3io.wp.backup.domain.archive_format import MANIFEST_MEMBER
from l3io.wp.backup.domain.database import EnsureOutcome, LoadResult
from l3io.wp.backup.domain.errors import ConfigLocationConflictError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.restore_harness import ARCHIVE_DB, TARGET_DB, wp_config, write_v1
from tests.restore_root_rig import FakeDatabase, Rig

_ARCHIVE_ID = f"{ARCHIVE_DB['DB_USER']}@{ARCHIVE_DB['DB_HOST']}/{ARCHIVE_DB['DB_NAME']}"


class _Killed(BaseException):
    """Not an ``Exception``: no handler in the executor treats it as orderly."""


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(tmp_path, monkeypatch)
    rig.config.unlink()
    rig.survey = dataclasses.replace(rig.survey, user_exists=False)
    archive_dir = rig.work / "parent-archive"
    archive_dir.mkdir()
    rig.archive_path = write_v1(archive_dir, config_role=True)
    return rig


def _parent(rig: Rig) -> Path:
    return rig.site_root.resolve().parent / "wp-config.php"


def _drive(rig: Rig, **changes: object) -> RestoreOutcome:
    archive = StorageTarget(scheme="file", path=PurePosixPath(rig.archive_path))
    return rig.drive(archive=archive, archive_database=_ARCHIVE_ID, **changes)


def _loads(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real executor; the fake database loads by saying so."""
    rig.real_executor()
    monkeypatch.setattr(
        FakeDatabase,
        "ensure_database",
        lambda self, *a, **k: EnsureOutcome(False, False, False, False, (), True),
        raising=False,
    )
    monkeypatch.setattr(
        FakeDatabase,
        "load_dump",
        lambda self, *a, **k: LoadResult(bytes=1, duration_ms=1.0, performed=True),
        raising=False,
    )


def test_the_header_and_the_survey_name_the_parent_file(rig: Rig) -> None:
    _drive(rig)
    parent = _parent(rig)
    inputs = rig.inputs
    assert inputs.relocation.lay_down_config is True
    assert inputs.relocation.config_file == PurePosixPath(parent)
    survey = rig.executions[-1]["survey_target"]
    assert survey().config_exists is False
    parent.write_text("<?php\n", encoding="utf-8")
    assert survey().config_exists is True
    assert inputs.header.wp_config_path == str(parent)
    actions = [a.text for a in rig.plans[-1].actions]
    assert any(f"lay down {parent}" in text for text in actions), actions


def test_a_dry_run_plans_the_lay_down_and_writes_nothing(rig: Rig) -> None:
    rig.real_executor()
    outcome = _drive(rig, dry_run=True)
    parent = _parent(rig)
    assert any(f"lay down {parent}" in a.text for a in outcome.actions)
    assert not parent.exists()
    assert not (rig.site_root / "wp-config.php").exists()


def test_a_kill_inside_the_cutover_resolves_into_the_parent_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _loads(rig, monkeypatch)
    real_set = WpConfigWriter.set_constants
    seen: list[str] = []

    def set_constants(
        self: WpConfigWriter, path: Path, values: Mapping[str, object]
    ) -> object:
        seen.append(str(path))
        if "DB_NAME" in values:
            raise _Killed
        return real_set(self, path, values)  # type: ignore[arg-type]

    monkeypatch.setattr(WpConfigWriter, "set_constants", set_constants)
    with pytest.raises(_Killed):
        _drive(rig)
    parent = _parent(rig)
    root_config = rig.site_root / "wp-config.php"
    assert seen, "the cutover never wrote"
    assert set(seen) == {str(parent)}
    assert parent.is_file()
    assert not root_config.exists()
    record = FileRunJournal(journal_directory(rig.site_root)).incomplete(
        PurePosixPath(rig.site_root.resolve())
    )
    assert record is not None
    assert record.header.wp_config_path == str(parent)
    load = record.header.load_database
    assert load is not None

    monkeypatch.setattr(WpConfigWriter, "set_constants", real_set)
    outcome = _drive(rig, resolve_run_id=record.header.run_id)
    assert outcome.resolved_run_id == record.header.run_id
    assert not root_config.exists()
    read = WpConfigWriter(SecretRegistry()).read_constants(parent, ("DB_NAME",))
    assert read["DB_NAME"] == load
    assert RestorePhase.LOAD_DUMP in rig.plans[0].phases
    assert rig.plans[0].mode is RestoreMode.ALL


def test_relocated_keys_land_in_the_parent_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F-08: a database relocation's cutover keys go to the laid-down file."""
    _loads(rig, monkeypatch)
    flags = dataclasses.replace(
        RelocationFlags.none(), new_db_name="e007_s01_002_relocated"
    )
    _drive(rig, relocation=flags)
    parent = _parent(rig)
    read = WpConfigWriter(SecretRegistry()).read_constants(parent, ("DB_NAME",))
    assert read["DB_NAME"] == "e007_s01_002_relocated"
    assert not (rig.site_root / "wp-config.php").exists()


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_shadowed_parent_file_is_refused_before_any_effect(
    rig: Rig, mode: RestoreMode
) -> None:
    """BL-E003-056 through the root: REFUSED, no snapshot, both files unchanged."""
    root_config = rig.site_root / "wp-config.php"
    root_config.write_text(wp_config(TARGET_DB), encoding="utf-8")
    parent = _parent(rig)
    parent.write_text(
        "<?php\n// a parent file WordPress never reads\n", encoding="utf-8"
    )
    before = {p: p.read_bytes() for p in (root_config, parent)}
    with pytest.raises(ConfigLocationConflictError) as caught:
        _drive(rig, mode=mode)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.path == PurePosixPath(parent)
    assert not rig.executions
    assert "backup" not in rig.events
    assert {p: p.read_bytes() for p in before} == before


# --------------------------------------------------------------------------
# No recorded location: the config goes to the default, <root>/wp-config.php
# --------------------------------------------------------------------------


def _v0_with_config(work: Path) -> Path:
    """A v0 Archive whose ``wp-root/`` holds a ``wp-config.php`` (no Manifest)."""
    path = work / "v0-config.tar.gz"
    members = (
        ("wp-root/index.php", b"<?php // v0\n"),
        ("wp-root/wp-config.php", wp_config(ARCHIVE_DB).encode()),
        ("database.sql", b"CREATE TABLE `wp_v0` (`id` int);\n"),
    )
    with tarfile.open(path, "w:gz") as tar:
        top = tarfile.TarInfo("wp-root")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o640
            tar.addfile(info, io.BytesIO(data))
    return path


def _without_config_path(source: Path, destination: Path) -> Path:
    """``source`` with no ``config_path`` in its plain outer Manifest."""
    with tarfile.open(source) as outer, tarfile.open(destination, "w") as out:
        for info in outer.getmembers():
            reader = outer.extractfile(info)
            assert reader is not None
            data = reader.read()
            if info.name == MANIFEST_MEMBER:
                document = json.loads(data)
                document.pop("config_path", None)
                data = json.dumps(document).encode()
                info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return destination


@pytest.mark.parametrize("kind", ["v0-tree", "v1-unrecorded"])
def test_an_archive_with_no_recorded_location_restores_its_config_at_the_root(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """``make ci`` regression: the default location gets the Archive's config."""
    if kind == "v0-tree":
        rig.archive_path = _v0_with_config(rig.work)
    else:
        rig.archive_path = _without_config_path(
            rig.archive_path, rig.work / "unrecorded.tar.gz"
        )
    _loads(rig, monkeypatch)
    _drive(rig)
    root_config = rig.site_root / "wp-config.php"
    assert root_config.is_file()
    assert not _parent(rig).exists()
    record = rig.inputs.header
    assert record.wp_config_path == str(rig.site_root.resolve() / "wp-config.php")
    read = WpConfigWriter(SecretRegistry()).read_constants(root_config, ("DB_NAME",))
    assert read["DB_NAME"] == record.load_database
