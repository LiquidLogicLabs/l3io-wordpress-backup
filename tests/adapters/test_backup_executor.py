"""AD-20/AD-12 — the plan executor and ``wpbackup2 backup``, against a database double.

The live-database path is ``tests/integration/test_backup_end_to_end.py``.
"""

from __future__ import annotations

import os
import stat
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.conftest import pipeline_events

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters import tar
from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    locate_wp_config,
)
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.tar import (
    SiteRootCollection,
    TarArchiveWriter,
    verify_or_raise,
)
from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.cli import adapter_sets, main
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.core.backup import BackupPlan, BackupStep, build_backup_plan
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    MemberRole,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.database import DatabaseLocation, DumpResult, Readiness
from l3io.wp.backup.domain.errors import (
    ArchiveWriteError,
    BackupError,
    DatabaseUnreachableError,
    DumpFailedError,
    UsageError,
)
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
DUMP_TEXT = b"-- dump\nCREATE TABLE wp_options (id INT);\n"


class FakeDatabase:
    """``DatabaseCommand`` double: writes a fixed Dump at ``0600``, or fails.

    ``error`` replaces the default ``DumpFailedError`` when ``fail`` is set;
    ``text`` is the Dump written (empty for a 0-byte Dump).
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        error: BackupError | None = None,
        text: bytes = DUMP_TEXT,
    ) -> None:
        self.fail = fail
        self.error = error
        self.text = text
        self.calls: list[tuple[DatabaseLocation, Path]] = []

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        return Readiness(ready=True)

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        self.calls.append((location, destination))
        if self.fail:
            if self.error is not None:
                raise self.error
            msg = "server went away"
            raise DumpFailedError(msg, database="wp")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(self.text)
        return DumpResult(bytes=len(self.text), duration_ms=1.0)


def _plan(
    site: SiteDescriptor, scope: BackupScope, target: StorageTarget, *, encrypted: bool
) -> BackupPlan:
    return build_backup_plan(
        site,
        scope,
        target,
        encrypted=encrypted,
        exclusion_source="none",
        exclusion_patterns=(),
    )


def _site(site_root: Path) -> SiteDescriptor:
    return SiteDescriptor(
        site_root=PurePosixPath(site_root),
        db_host="db.invalid",
        db_port=3306,
        db_name="wptest",
        table_prefix="wp_",
        site_url=None,
        home_url=None,
        credential_source="wp-config.php",
    )


def _run(
    site_root: Path,
    final: Path,
    workspace: Path,
    *,
    database: FakeDatabase | None = None,
    verify: Callable[[Path], None] = verify_or_raise,
    cipher: object = None,
) -> backup_cli.BackupResult:
    site = _site(site_root)
    target = StorageTarget(scheme="file", path=PurePosixPath(final))
    plan = _plan(site, BackupScope.ALL, target, encrypted=cipher is not None)
    return backup_cli.execute_backup_plan(
        plan,
        site=site,
        config=locate_wp_config(site_root, stated=None),
        workspace=workspace,
        database=database or FakeDatabase(),
        store=LocalArchiveStore(verify=verify),
        writer=TarArchiveWriter(),
        cipher=cipher,  # type: ignore[arg-type]
        exclusion=INCLUDE_EVERYTHING,
        origin=None,
        now=NOW,
    )


def test_a_backup_publishes_one_archive_and_a_record_per_step(
    tmp_path: Path,
    site_root: Path,
    workspace: Path,
    archive_reader: Callable[..., Any],
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    final = out / "site.tar.gz"
    with capture_logs() as logs:
        result = _run(site_root, final, workspace)
    assert result.path == PurePosixPath(final)
    assert result.bytes == final.stat().st_size
    assert result.encrypted is False
    assert list(out.iterdir()) == [final]
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    read = archive_reader(final)
    assert read.outer == [MANIFEST_MEMBER, PAYLOAD_MEMBER]
    assert read.inner[0].name.startswith(MemberRole.DUMP.value + "/")
    dump_name = read.inner[0].name
    assert read.contents[dump_name] == DUMP_TEXT
    assert read.manifest.dump_present is True
    assert read.manifest.backup_scope == "all"
    # BL-E002-005: ISO-8601 UTC, whole seconds, Z suffix.
    assert read.manifest.created_at == "2026-11-02T12:00:00Z"
    assert read.manifest.config_path == "wp-config.php"
    assert read.manifest.exclusions is not None
    assert read.manifest.exclusions.source == "none"
    assert "credential_source" not in read.manifest.source
    records = [e for e in logs if e["event"] == "phase_completed"]
    assert [e["phase"] for e in records] == [
        s.value for s in _plan(
            _site(site_root), BackupScope.ALL,
            StorageTarget("file", PurePosixPath(final)), encrypted=False,
        ).steps
    ]  # fmt: skip
    for record in records:
        assert set(record) >= {"phase", "duration_ms", "bytes", "outcome", "exit_code"}
        assert (record["outcome"], record["exit_code"]) == ("ok", 0)
    done = [e for e in logs if e["event"] == "backup_completed"]
    assert done[0]["format_version"] == 1
    assert done[0]["members"]["file"] >= 3


def test_an_encrypted_backup_emits_encrypt_inside_the_write(
    tmp_path: Path,
    site_root: Path,
    workspace: Path,
    cipher: object,
) -> None:
    final = tmp_path / "site.tar.gz"
    with capture_logs() as logs:
        result = _run(site_root, final, workspace, cipher=cipher)
    assert result.encrypted is True
    phases = [e["phase"] for e in logs if e["event"] == "phase_completed"]
    assert sorted(phases) == sorted(s.value for s in BackupStep)
    assert cipher.seen == ["check"]
    assert cipher.encrypt_calls == 1


def test_a_cipher_without_an_encrypt_step_is_refused(
    tmp_path: Path, site_root: Path, workspace: Path, cipher: object
) -> None:
    site = _site(site_root)
    target = StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tar"))
    plan = _plan(site, BackupScope.ALL, target, encrypted=False)
    with pytest.raises(UsageError):
        backup_cli.execute_backup_plan(
            plan,
            site=site,
            config=LocatedConfig(path=site_root / "wp-config.php", location=None),
            workspace=workspace,
            database=FakeDatabase(),
            store=LocalArchiveStore(verify=verify_or_raise),
            writer=TarArchiveWriter(),
            cipher=cipher,
            exclusion=INCLUDE_EVERYTHING,
            origin=None,
            now=NOW,
        )


def test_a_failing_verifier_leaves_no_final_name(
    tmp_path: Path, site_root: Path, workspace: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    final = out / "site.tar.gz"

    class Refused(BackupError):
        pass

    def refuse(path: Path) -> None:
        msg = "forced verifier failure"
        raise Refused(msg)

    with pytest.raises(Refused):
        _run(site_root, final, workspace, verify=refuse)
    assert list(out.iterdir()) == []


def test_a_failed_dump_stages_nothing(
    tmp_path: Path, site_root: Path, workspace: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(DumpFailedError):
        _run(site_root, out / "a.tar", workspace, database=FakeDatabase(fail=True))
    assert list(out.iterdir()) == []


def test_a_cipher_failure_leaves_the_destination_empty(
    tmp_path: Path, site_root: Path, workspace: Path, cipher_factory: type
) -> None:
    """BL-E002-042: the executor publishes nothing when the cipher fails."""
    (site_root / "big.bin").write_bytes(os.urandom(256 * 1024))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ArchiveWriteError):
        _run(
            site_root, out / "a.tar.gz", workspace, cipher=cipher_factory(fail_after=1)
        )
    assert list(out.iterdir()) == []


def test_an_empty_dump_is_archived_as_an_empty_member(
    tmp_path: Path,
    site_root: Path,
    workspace: Path,
    archive_reader: Callable[..., Any],
) -> None:
    """BL-E002-042: a 0-byte Dump is a valid, published, empty dump member."""
    final = tmp_path / "site.tar.gz"
    _run(site_root, final, workspace, database=FakeDatabase(text=b""))
    read = archive_reader(final)
    (dump_name,) = [
        info.name
        for info in read.inner
        if info.name.startswith(MemberRole.DUMP.value + "/")
    ]
    assert read.contents[dump_name] == b""
    assert read.manifest.dump_present is True


def test_a_site_root_holding_only_wp_config_is_archived(
    tmp_path: Path, workspace: Path, archive_reader: Callable[..., Any]
) -> None:
    """BL-E002-042: a Site Root of nothing but wp-config.php, end to end."""
    site_root = tmp_path / "bare"
    site_root.mkdir()
    (site_root / "wp-config.php").write_text("<?php\n// only\n", encoding="utf-8")
    final = tmp_path / "bare.tar.gz"
    result = _run(site_root, final, workspace)
    assert result.path == PurePosixPath(final)
    read = archive_reader(final)
    assert _roles(read.inner) == {
        MemberRole.DUMP.value,
        MemberRole.CONFIG.value,
        MemberRole.SITE_ROOT.value,
    }
    config = f"{MemberRole.CONFIG.value}/wp-config.php"
    assert read.contents[config] == b"<?php\n// only\n"
    assert read.manifest.config_path == "wp-config.php"


def test_a_site_root_collection_failure_discards_the_staged_archive(
    tmp_path: Path,
    site_root: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-E002-080 (R12): collection runs inside the writer, after staging, so
    a collection failure stages and then discards; nothing is left at the
    Storage Target."""
    out = tmp_path / "out"
    out.mkdir()
    staged: list[list[Path]] = []

    def collect_fails(root: Path, policy: ExclusionPolicy) -> SiteRootCollection:
        staged.append(sorted(out.iterdir()))
        msg = "unreadable"
        raise PermissionError(msg)

    monkeypatch.setattr(tar, "collect_site_root", collect_fails)
    with pytest.raises(PermissionError):
        _run(site_root, out / "a.tar.gz", workspace)
    assert staged
    assert staged[0], "collection ran before staging"
    assert list(out.iterdir()) == []


def test_a_write_failure_discards_the_staged_archive(
    tmp_path: Path,
    site_root: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (site_root / "vanishing.txt").write_text("x")
    original = tar.collect_site_root

    def collect_then_delete(root: Path, policy: ExclusionPolicy) -> SiteRootCollection:
        collection = original(root, policy)
        (root / "vanishing.txt").unlink()
        return collection

    monkeypatch.setattr(tar, "collect_site_root", collect_then_delete)
    with pytest.raises(BackupError) as caught:
        _run(site_root, out / "a.tar", workspace)
    assert "vanishing.txt" in str(caught.value)
    assert list(out.iterdir()) == []
    assert list(workspace.iterdir()) == [workspace / "database.sql"]


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


@pytest.fixture
def cli_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    root = tmp_path / "www"
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\n"
        "define('DB_NAME', 'wptest');\n"
        "define('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n")
    database = FakeDatabase()
    monkeypatch.setattr(
        adapter_sets,
        "WpDatabase",
        lambda credentials, *, secrets, admin=None, db_tls=None: database,
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    return root, database


def _snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(p.relative_to(root)): (
            p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None,
        )
        for p in sorted(root.rglob("*"))
    }


def test_the_backup_command_publishes_and_leaves_the_site_unchanged(
    tmp_path: Path, cli_site: tuple[Path, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root, database = cli_site
    final = tmp_path / "out.tar.gz"
    before = _snapshot(root)
    code = main(["backup", "--wp-dir", str(root), "--archive", str(final)])
    assert code == 0, capsys.readouterr().err
    assert final.is_file()
    assert _snapshot(root) == before
    assert len(database.calls) == 1
    assert database.calls[0][1].parent.name.startswith(WORKSPACE_PREFIX)
    assert list((tmp_path / "tmp").iterdir()) == []  # the workspace is gone
    verify_or_raise(final)


def test_an_existing_archive_is_refused_before_any_dump(
    tmp_path: Path, cli_site: tuple[Path, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root, database = cli_site
    final = tmp_path / "out.tar.gz"
    final.write_bytes(b"previous")
    code = main(["backup", "--wp-dir", str(root), "--archive", f"file://{final}"])
    assert code != 0
    assert database.calls == []
    assert final.read_bytes() == b"previous"
    assert "already exists" in capsys.readouterr().err
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_failed_backup_returns_non_zero_and_leaves_nothing(
    tmp_path: Path, cli_site: tuple[Path, Any]
) -> None:
    root, database = cli_site
    database.fail = True
    final = tmp_path / "out.tar.gz"
    assert main(["backup", "--wp-dir", str(root), "--archive", str(final)]) != 0
    assert not final.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".out")] == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_the_default_archive_name_is_in_the_current_directory(
    tmp_path: Path, cli_site: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = cli_site
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    assert main(["backup", "--wp-dir", str(root)]) == 0
    (archive,) = list(here.iterdir())
    assert archive.name.startswith("wpbak-wptest-")


def test_a_stated_wp_config_is_used(tmp_path: Path, cli_site: tuple[Path, Any]) -> None:
    root, _ = cli_site
    stated = tmp_path / "elsewhere.php"
    stated.write_bytes((root / "wp-config.php").read_bytes())
    (root / "wp-config.php").unlink()
    final = tmp_path / "out.tar.gz"
    code = main(
        [
            "backup",
            "--wp-dir",
            str(root),
            "--archive",
            str(final),
            "--wp-config",
            str(stated),
        ]
    )
    assert code == 0
    verify_or_raise(final)


def test_a_missing_site_root_returns_non_zero(tmp_path: Path) -> None:
    assert main(["backup", "--wp-dir", str(tmp_path / "missing")]) != 0


@pytest.mark.parametrize("name", ["site#1.tar.gz", "site?.tar.gz"])
def test_a_bare_archive_path_is_taken_as_given(
    tmp_path: Path, cli_site: tuple[Path, Any], name: str
) -> None:
    root, _ = cli_site
    final = tmp_path / name
    assert main(["backup", "--wp-dir", str(root), "--archive", str(final)]) == 0
    assert final.is_file()


def test_a_file_url_is_percent_decoded(
    tmp_path: Path, cli_site: tuple[Path, Any]
) -> None:
    root, _ = cli_site
    url = f"file://{tmp_path}/a%20b.tar.gz"
    assert main(["backup", "--wp-dir", str(root), "--archive", url]) == 0
    assert (tmp_path / "a b.tar.gz").is_file()


@pytest.mark.parametrize(
    "url",
    ["file://host{path}", "file://{path}?x=1", "file://{path}#frag"],
)
def test_a_url_with_a_host_query_or_fragment_is_refused(
    tmp_path: Path,
    cli_site: tuple[Path, Any],
    url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, database = cli_site
    text = url.format(path=tmp_path / "x.tar.gz")
    assert main(["backup", "--wp-dir", str(root), "--archive", text]) != 0
    assert database.calls == []
    assert "not supported" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir() if "x.tar" in p.name] == []


def test_a_nul_in_a_file_url_is_a_usage_error_before_any_dump(
    tmp_path: Path, cli_site: tuple[Path, Any]
) -> None:
    root, database = cli_site
    url = f"file://{tmp_path}/x%00.tar"
    assert main(["backup", "--wp-dir", str(root), "--archive", url]) != 0
    assert database.calls == []


# --------------------------------------------------------------------------
# FR-2 — the Backup Scope selects the steps, never the Archive format
# --------------------------------------------------------------------------


class RefusingCredentials:
    """A ``CredentialSource`` double: any attempt to resolve it fails the test."""

    source = "wp-config.php"

    def resolve(self) -> object:
        msg = "a files backup resolved database credentials"
        raise AssertionError(msg)


def _roles(inner: list[tarfile.TarInfo]) -> set[str]:
    return {info.name.split("/")[0] for info in inner}


class RefusingDatabase:
    """Bound into a files Run's adapter set; any use of it fails the test."""

    def check_dump(self, location: object) -> object:
        msg = "a files backup used the database adapter"
        raise AssertionError(msg)

    def dump_to(self, location: object, destination: Path) -> object:
        msg = "a files backup used the database adapter"
        raise AssertionError(msg)


def test_a_files_backup_resolves_no_credential_and_binds_no_database(
    tmp_path: Path,
    cli_site: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    archive_reader: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The set is bound once per Run (ADR-0016), so the database adapter is
    constructed; it is never handed to the executor, never resolves and never
    connects (FR-2)."""
    root, _ = cli_site
    real = backup_cli.site_descriptor_from_wp_config

    def descriptor(
        site_root: Path, *, config_file: Path, **kwargs: object
    ) -> tuple[Any, Any]:
        site, _ = real(site_root, config_file=config_file, **kwargs)  # type: ignore[arg-type]
        return site, RefusingCredentials()

    monkeypatch.setattr(backup_cli, "site_descriptor_from_wp_config", descriptor)
    monkeypatch.setattr(
        adapter_sets,
        "WpDatabase",
        lambda credentials, *, secrets, admin=None, db_tls=None: RefusingDatabase(),
    )
    final = tmp_path / "files.tar.gz"
    argv = ["backup", "--wp-dir", str(root), "--archive", str(final)]
    code = main([*argv, "--mode", "files"])
    logs = pipeline_events(caplog)
    assert code == 0, capsys.readouterr().err
    read = archive_reader(final)
    assert read.manifest.backup_scope == "files"
    assert read.manifest.dump_present is False
    assert _roles(read.inner) == {MemberRole.CONFIG.value, MemberRole.SITE_ROOT.value}
    assert [e["phase"] for e in logs if e["event"] == "phase_completed"] == [
        "COLLECT_SITE_ROOT", "WRITE_ARCHIVE", "VERIFY", "PUBLISH",
    ]  # fmt: skip


def test_a_db_backup_is_a_full_archive_with_no_site_root(
    tmp_path: Path,
    cli_site: tuple[Path, Any],
    archive_reader: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    root, database = cli_site
    out = tmp_path / "out"
    out.mkdir()
    final = out / "db.tar.gz"
    code = main(
        ["backup", "--wp-dir", str(root), "--archive", str(final), "--mode", "db"]
    )
    logs = pipeline_events(caplog)
    assert code == 0, capsys.readouterr().err
    assert list(out.iterdir()) == [final]
    assert list((tmp_path / "tmp").iterdir()) == []
    assert len(database.calls) == 1
    read = archive_reader(final)
    assert read.manifest.backup_scope == "database"
    assert read.manifest.dump_present is True
    assert _roles(read.inner) == {MemberRole.DUMP.value, MemberRole.CONFIG.value}
    started = [e for e in logs if e["event"] == "backup_started"]
    assert [e["backup_scope"] for e in started] == ["database"]
    assert [e["phase"] for e in logs if e["event"] == "phase_completed"] == [
        "DUMP", "WRITE_ARCHIVE", "VERIFY", "PUBLISH",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "error",
    [
        DatabaseUnreachableError("db.invalid", 3306),
        DumpFailedError("server went away", database="wp"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_failed_db_backup_leaves_no_archive_and_no_loose_dump(
    tmp_path: Path, cli_site: tuple[Path, Any], error: BackupError
) -> None:
    """The AC's unreachable database, and a dump that fails, exit with the
    error's own code (BL-E002-047)."""
    root, database = cli_site
    database.fail = True
    database.error = error
    out = tmp_path / "out"
    out.mkdir()
    final = out / "db.tar.gz"
    argv = ["backup", "--wp-dir", str(root), "--archive", str(final), "--mode", "db"]
    assert main(argv) == int(type(error).exit_code)
    assert list(out.iterdir()) == []
    assert list((tmp_path / "tmp").iterdir()) == []
    assert not list(root.rglob("*.sql"))


@pytest.mark.parametrize(("mode", "checked"), [("db", 1), ("all", 1), ("files", 0)])
def test_the_early_credential_check_runs_for_every_dumping_mode(
    tmp_path: Path,
    cli_site: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    checked: int,
) -> None:
    """BL-E002-046: ``require_wp_config_credentials`` runs before any dump for
    ``db``/``all`` (and not for ``files``)."""
    root, database = cli_site
    dumps_at_check: list[int] = []
    real = backup_cli.require_wp_config_credentials

    def spy(credentials: object, **kwargs: object) -> None:
        dumps_at_check.append(len(database.calls))
        real(credentials, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_cli, "require_wp_config_credentials", spy)
    final = tmp_path / f"{mode}.tar.gz"
    argv = ["backup", "--wp-dir", str(root), "--archive", str(final), "--mode", mode]
    assert main(argv) == 0
    assert dumps_at_check == [0] * checked


@pytest.mark.parametrize("scope", list(BackupScope))
def test_the_database_binding_must_match_the_plans_dump_step(
    tmp_path: Path, site_root: Path, workspace: Path, scope: BackupScope
) -> None:
    site = _site(site_root)
    target = StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tar"))
    plan = _plan(site, scope, target, encrypted=False)
    wrong = None if BackupStep.DUMP in plan.steps else FakeDatabase()
    with pytest.raises(UsageError):
        backup_cli.execute_backup_plan(
            plan,
            site=site,
            config=locate_wp_config(site_root, stated=None),
            workspace=workspace,
            database=wrong,
            store=LocalArchiveStore(verify=verify_or_raise),
            writer=TarArchiveWriter(),
            cipher=None,
            exclusion=INCLUDE_EVERYTHING,
            origin=None,
            now=NOW,
        )
    assert not (tmp_path / "a.tar").exists()


def test_an_exclusion_policy_other_than_the_plans_is_refused(
    tmp_path: Path, site_root: Path, workspace: Path
) -> None:
    """R8: the Manifest records the plan's set; the writer must apply that set."""
    import dataclasses

    site = _site(site_root)
    target = StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tar"))
    plan = dataclasses.replace(
        _plan(site, BackupScope.ALL, target, encrypted=False),
        exclusion_source="default",
        exclusion_patterns=("wp-content/cache/",),
    )
    with pytest.raises(UsageError, match="exclusion"):
        backup_cli.execute_backup_plan(
            plan,
            site=site,
            config=LocatedConfig(path=site_root / "wp-config.php", location=None),
            workspace=workspace,
            database=FakeDatabase(),
            store=LocalArchiveStore(verify=verify_or_raise),
            writer=TarArchiveWriter(),
            cipher=None,
            exclusion=INCLUDE_EVERYTHING,
            origin=None,
            now=NOW,
        )
    assert not (tmp_path / "a.tar").exists()
