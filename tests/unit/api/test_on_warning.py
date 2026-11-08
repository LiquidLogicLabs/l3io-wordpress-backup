"""Owner decision 2026-11-07 (E006 closure decision 2) -- ``on_warning``.

A library caller's ``on_warning`` receives every operator notice the restore
driver gives (``RunWarning``) instead of stderr; without it stderr is as it
was. The set of notices is derived from the code: every ``RunWarning(code=...)``
construction under ``src/l3io/wp/backup/``. A guard checks that no function
the drivers reach writes to stderr except through ``cli/output.emit_warning``.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup import api
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.api import operations
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.output import RunWarning
from l3io.wp.backup.core.restore import SnapshotResult
from l3io.wp.backup.domain.archive_format import Integrity
from tests.restore_root_rig import Rig
from tests.root_mode import running_as_root

PACKAGE = Path(api.__file__).resolve().parents[1]
#: The modules whose drivers the library calls.
DRIVER_MODULES = ("cli/backup.py", "cli/restore.py", "cli/verify.py", "cli/doctor.py")


def _codes(root: Path) -> set[str]:
    found = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RunWarning"
            ):
                for keyword in node.keywords:
                    if keyword.arg == "code" and isinstance(
                        keyword.value, ast.Constant
                    ):
                        found.add(str(keyword.value.value))
    return found


def _notices_without_a_warning_record(source: str) -> set[str]:
    """``RunWarning`` codes in ``source`` no ``_log.warning``/``error`` names.

    Under ``--log-format json`` ``emit_warning`` prints nothing: the caller's
    record is the notice's only trace on stderr, so it must pass the default
    level and ``--log-level WARNING``.
    """
    tree = ast.parse(source)
    codes = _codes_in(tree)
    logged = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"warning", "error", "critical"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            logged.add(str(node.args[0].value))
    return codes - logged


def _codes_in(tree: ast.AST) -> set[str]:
    return {
        str(keyword.value.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunWarning"
        for keyword in node.keywords
        if keyword.arg == "code" and isinstance(keyword.value, ast.Constant)
    }


def test_every_notice_is_logged_at_warning_or_above() -> None:
    modules = [
        path
        for path in PACKAGE.rglob("*.py")
        if _codes_in(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert modules
    for path in modules:
        source = path.read_text(encoding="utf-8")
        assert _notices_without_a_warning_record(source) == set(), path


def test_a_notice_logged_at_info_is_found() -> None:
    source = (PACKAGE / "cli" / "restore.py").read_text(encoding="utf-8")
    planted = source.replace(
        '_log.warning("safety_snapshot_encrypted"',
        '_log.info("safety_snapshot_encrypted"',
        1,
    )
    assert planted != source
    assert _notices_without_a_warning_record(planted) == {"safety_snapshot_encrypted"}


class _Cipher:
    """A ``PayloadCipher`` that is never asked to run (the executor is faked)."""

    def required_tools(self) -> tuple[()]:
        return ()

    def check(self) -> None: ...

    def encrypt(self, reader: object, writer: object) -> None: ...

    def decrypt(self, reader: object, writer: object) -> None: ...


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(tmp_path, monkeypatch)
    real_result = restore_mod.snapshot_result

    def performed(path: PurePosixPath, **kwargs: object) -> SnapshotResult:
        result = real_result(path, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, performed=True, integrity=Integrity.VERIFIED)

    monkeypatch.setattr(restore_mod, "snapshot_result", performed)

    def snapshot_then_stop(*args: object, **kwargs: object) -> object:
        from tests.restore_root_rig import BackupScope

        rig.take_snapshot(BackupScope.ALL)
        raise _Stop

    rig.execute = snapshot_then_stop
    return rig


class _Stop(Exception):
    """Ends the faked executor once the snapshot is taken."""


def _drive(rig: Rig, sink: Callable[[RunWarning], None] | None, **changes: Any) -> None:  # noqa: ANN401
    with capture_logs(), pytest.raises(_Stop):
        restore_mod.drive_restore(
            rig.request(**changes), ctx=None, secrets=None, on_warning=sink
        )


def test_the_scan_finds_the_notices() -> None:
    assert _codes(PACKAGE) >= {
        "archive_unauthenticated",
        "safety_snapshot_unencrypted",
        "safety_snapshot_encrypted",
    }
    assert _codes(PACKAGE / "cli") == _codes(PACKAGE)


def _site(root: Path) -> Path:
    root.mkdir()
    (root / "wp-config.php").write_text(
        "<?php\ndefine('DB_NAME', 'wp');\ndefine('DB_USER', 'wp');\n"
        "define('DB_PASSWORD', 'not-a-real-secret');\n"
        "define('DB_HOST', 'localhost');\n$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    return root


def _every_entry_points_notices(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sink: Callable[[RunWarning], None],
) -> None:
    """Drive every notice the package defines, each through its entry point's sink.

    BL-E008-012 (owner decision 2026-11-07): ``restore``, ``backup`` and the
    CLI-flag notices all take the sink, so one derived set covers them all.
    """
    from l3io.wp.backup.cli.credentials import new_db_password
    from l3io.wp.backup.cli.passphrase import (
        PASSPHRASE_ENV,
        ResolvedPassphrase,
        note_short_restore_passphrase,
    )
    from l3io.wp.backup.domain.secret import Secret

    _drive(rig, sink)
    _drive(rig, sink, cipher=_Cipher(), allow_unencrypted=True)
    monkeypatch.setattr(restore_mod, "_running_as_root", lambda: True)
    _drive(rig, sink, allow_root=True)
    monkeypatch.undo()
    note_short_restore_passphrase(
        ResolvedPassphrase(Secret("sentinel9"), "argument"), sink
    )
    new_db_password(
        explicit="sentinel-flag-value-29a",
        file=None,
        environ={},
        secrets=SecretRegistry(),
        user_given=True,
        on_warning=sink,
    )
    site = _site(tmp_path / "site")
    with monkeypatch.context() as patch:
        patch.setenv(PASSPHRASE_ENV, "sentinel-env-passphrase-29")
        with capture_logs():
            api.backup(
                site,
                scope=api.BackupScope.FILES,
                target=site / "inside.tar.gz",
                allow_target_in_site_root=True,
                on_warning=sink,
            )


def test_every_notice_reaches_the_callback_and_nothing_reaches_stderr(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[RunWarning] = []
    _every_entry_points_notices(rig, tmp_path, monkeypatch, seen.append)
    assert {w.code for w in seen} == _codes(PACKAGE)
    assert capsys.readouterr().err == ""
    for warning in seen:
        assert warning.message
        assert all(isinstance(v, str) for v in warning.fields.values())


def test_without_a_callback_the_notices_go_to_stderr(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[RunWarning] = []
    _drive(rig, seen.append)
    capsys.readouterr()
    _drive(rig, None)
    err = capsys.readouterr().err.splitlines()
    # The snapshot path carries this Run's ID; the rest is the same text.
    assert [line.split(": /")[0] for line in err] == [
        w.message.split(": /")[0] for w in seen
    ]


def test_the_warning_records_are_logged_either_way(rig: Rig) -> None:
    for sink in (None, [].append):
        with capture_logs() as logs, pytest.raises(_Stop):
            restore_mod.drive_restore(
                rig.request(), ctx=None, secrets=None, on_warning=sink
            )
        events = {r["event"] for r in logs if r["log_level"] == "warning"}
        assert {"archive_unauthenticated", "safety_snapshot_unencrypted"} <= events


def test_a_callbacks_exception_propagates(rig: Rig) -> None:
    class Refused(Exception):
        pass

    def sink(warning: RunWarning) -> None:
        raise Refused(warning.code)

    # As root (CI) the rig passes allow_root, whose notice comes first.
    first = (
        "restore_running_as_root" if running_as_root() else "archive_unauthenticated"
    )
    with capture_logs(), pytest.raises(Refused, match=first):
        restore_mod.drive_restore(
            rig.request(), ctx=None, secrets=None, on_warning=sink
        )


def test_api_restore_hands_its_callback_to_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def drive(request: object, **kwargs: object) -> object:
        seen.append(kwargs["on_warning"])
        raise _Stop

    monkeypatch.setattr(operations, "drive_restore", drive)
    sink: Callable[[RunWarning], None] = [].append
    with pytest.raises(api.UnexpectedError):
        api.restore(
            tmp_path,
            archive=tmp_path / "a.tar.gz",
            mode=api.RestoreMode.FILES,
            overrides=api.OverrideSet(frozenset()),
            snapshot=api.SnapshotPolicy.SKIP,
            config_policy=api.ConfigPolicy.KEEP,
            on_warning=sink,
        )
    assert seen == [sink]
    for name in ("backup", "restore", "verify"):
        assert "on_warning" in inspect.signature(getattr(api, name)).parameters


# --------------------------------------------------------------------------
# The guard: no stderr write a driver reaches bypasses the sink
# --------------------------------------------------------------------------


def _stderr_writers_reached(source: str) -> list[str]:
    """Functions reached from a ``drive_*`` function (by name, in-module) that
    mention ``sys.stderr``."""
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    calls = {
        name: {
            c.func.id
            for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        for name, node in functions.items()
    }
    reached: set[str] = set()
    todo = [name for name in functions if name.startswith("drive_")]
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        todo += [c for c in calls.get(name, ()) if c in functions]
    return sorted(
        name
        for name in reached
        if any(
            isinstance(n, ast.Attribute) and n.attr == "stderr"
            for n in ast.walk(functions[name])
        )
    )


def test_no_driver_writes_to_stderr_but_through_the_sink() -> None:
    for module in DRIVER_MODULES:
        source = (PACKAGE / module).read_text(encoding="utf-8")
        assert _stderr_writers_reached(source) == [], module


def test_a_planted_stderr_write_in_a_driver_helper_is_found(tmp_path: Path) -> None:
    planted = (
        "import sys\n"
        "def drive_x():\n    helper()\n"
        "def helper():\n    print('x', file=sys.stderr)\n"
        "def run_x():\n    print('cli only', file=sys.stderr)\n"
    )
    assert _stderr_writers_reached(planted) == ["helper"]
    copy = tmp_path / "restore.py"
    shutil.copy(PACKAGE / "cli" / "restore.py", copy)
    text = copy.read_text(encoding="utf-8").replace(
        "        emit_warning(\n",
        "        print(file=sys.stderr)\n        emit_warning(\n",
        1,
    )
    assert _stderr_writers_reached(text) == ["drive_restore"]


def test_an_encrypted_snapshot_is_logged_as_well_as_warned(rig: Rig) -> None:
    with capture_logs() as logs, pytest.raises(_Stop):
        restore_mod.drive_restore(
            rig.request(cipher=_Cipher(), allow_unencrypted=True),
            ctx=None,
            secrets=None,
            on_warning=[].append,
        )
    (record,) = [r for r in logs if r["event"] == "safety_snapshot_encrypted"]
    assert record["log_level"] == "warning"
    assert record["path"]
