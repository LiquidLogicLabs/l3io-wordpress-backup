"""ADR-0019/AD-4 — a backup Dry Run leaves the Site and the Storage Target alone.

Against a temporary Site Root and a recording database double, ``wpbackup2
--whatIf backup``: leaves the Site Root byte-identical, writes nothing at the
Storage Target, reads no Site Root file but ``wp-config.php`` (credential
resolution is real, FR-19) — an ``open`` spy scoped to the Site Root — and
never calls ``write_archive``, ``collect_site_root`` or the verifier (spies on
the names the real adapters look up). The query ``check_dump`` runs; the
command ``dump_to`` does not. A refused ``--whatIf`` Run still prints its plan
on stdout, whatever ``--log-level`` says; a refused real Run prints nothing.

The executor and the Safety Snapshot entry point live in ``cli/backup.py``,
outside CI's AD-4 grep, so AST checks over that module carry the rule there:

- it imports no adapter module but those on :data:`ADAPTER_ALLOWANCE` (the
  modules are discovered by walking ``adapters/``), and from
  ``adapters.database`` only the query/locate names on that allowance, each
  checked against the module's ``__all__`` — so no writer, dump or store
  implementation (``write_archive``, ``WpDatabase``, ``LocalArchiveStore``,
  ``verify_or_raise``) is reachable except through the bound ports;
- it uses ``os`` only for :data:`OS_ALLOWANCE` (no ``os.replace``);
- no function or class it defines, other than the selection code on
  :data:`SELECTION`, names ``dry_run`` or ``what_if`` — as a parameter, a
  name, an attribute or a keyword; the scope is every top-level definition
  in the module, so a new executor helper is covered;
- it calls neither the writer nor the traversal directly.

Each check is run over a planted module carrying its violation.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from l3io.wp.backup import adapters as adapters_package
from l3io.wp.backup.adapters import noop, tar
from l3io.wp.backup.adapters.workspace import WORKSPACE_PREFIX
from l3io.wp.backup.cli import adapter_sets, main
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import pipeline_events
from tests.unit.cli.conftest import FakeDatabase

EFFECTS = ("write_archive", "collect_site_root")
ADAPTERS = "l3io.wp.backup.adapters"
#: The adapter modules ``cli/backup.py`` may import from, and the names it may
#: take: queries and locators, observation, and the registry type.
ADAPTER_ALLOWANCE: dict[str, frozenset[str]] = {
    f"{ADAPTERS}.database": frozenset(
        {
            "LocatedConfig",
            # BL-E002-063 (owner decision 2026-11-07): a pure host query.
            "database_host_is_local",
            "locate_wp_config",
            "require_wp_config_credentials",
            "site_descriptor_from_wp_config",
        }
    ),
    # E008-S01-001: the cipher factory and the logged exclusion wrapper; a
    # cipher and a policy are per-call arguments, neither writes.
    f"{ADAPTERS}.crypto": frozenset({"PassphraseCipher", "build_payload_cipher"}),
    # BL-E004-045: log_path_excluded writes one DEBUG record, nothing else.
    f"{ADAPTERS}.exclusions": frozenset({"LoggedExclusionPolicy", "log_path_excluded"}),
    f"{ADAPTERS}.phase": frozenset({"phase"}),
    f"{ADAPTERS}.redaction": frozenset({"SecretRegistry"}),
}
OS_ALLOWANCE = frozenset({"environ"})
#: The definitions that select the Dry Run (AD-4) and so may name it.
SELECTION = frozenset({"BackupRequest", "drive_backup", "run_backup"})
DRY_RUN_NAMES = frozenset({"dry_run", "what_if"})


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    return {
        str(p.relative_to(root)): (
            p.lstat().st_mtime_ns,
            p.lstat().st_size,
            p.read_bytes() if p.is_file() else None,
        )
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    called: dict[str, list[object]] = {name: [] for name in (*EFFECTS, "verify")}

    def spy(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: object, **kwargs: object) -> object:
            called[name].append(args)
            return real(*args, **kwargs)

        return wrapper

    for name in EFFECTS:
        monkeypatch.setattr(tar, name, spy(name, getattr(tar, name)))
    # real_adapters binds the verifier into the store; patch where it is read.
    monkeypatch.setattr(
        adapter_sets, "verify_or_raise", spy("verify", adapter_sets.verify_or_raise)
    )
    return called


@pytest.fixture
def opened(site: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Site Root path opened or listed, by any of the ways to do it."""
    seen: list[str] = []
    root = str(site)

    def note(path: object) -> None:
        text = os.fsdecode(path) if isinstance(path, str | bytes | os.PathLike) else ""
        if text.startswith(root):
            seen.append(os.path.relpath(text, root))

    def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: object, *args: object, **kwargs: object) -> object:
            note(path)
            return real(path, *args, **kwargs)

        return wrapper

    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", wrap(real_open))
    monkeypatch.setattr(io, "open", wrap(real_open))
    monkeypatch.setattr(os, "open", wrap(os.open))
    monkeypatch.setattr(os, "scandir", wrap(os.scandir))
    return seen


@pytest.mark.parametrize("spelling", ["--whatIf", "--what-if"])
def test_a_dry_run_changes_nothing_and_reads_no_site_content(
    spelling: str,
    site: Path,
    tmp_path: Path,
    workdir: Path,
    fake_database: FakeDatabase,
    spies: dict[str, list[object]],
    opened: list[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--what-if`` before the action is the same Dry Run (BL-E002-079); the
    workspace is observed while the Run is active, after the no-op dump and
    when the no-op writer runs (BL-E002-078)."""
    while_active: list[list[str]] = []
    real_write = noop.NoopArchiveWriter.write

    def write(self: noop.NoopArchiveWriter, *args: object, **kwargs: object) -> object:
        while_active.append(
            sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*"))
        )
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(noop.NoopArchiveWriter, "write", write)
    (site / "wp-content").mkdir()
    (site / "wp-content" / "big.bin").write_bytes(b"\x00" * 4096)
    out = tmp_path / "out"
    out.mkdir()
    (out / "earlier.tar.gz").write_bytes(b"an earlier archive")
    before_site, before_out = _snapshot(site), _snapshot(out)
    opened.clear()

    code = main(
        [spelling, "backup", "--wp-dir", str(site), "--archive", str(out / "a")]
    )
    logs = pipeline_events(caplog)

    touched = list(opened)  # before the snapshot below reads the tree itself
    printed = capsys.readouterr()
    assert code == 0, printed.err
    assert _snapshot(site) == before_site
    assert _snapshot(out) == before_out
    assert list(workdir.iterdir()) == []  # the workspace was made and removed
    # While active it existed and the no-op commands wrote nothing into it.
    ((workspace,),) = while_active
    assert workspace.startswith(WORKSPACE_PREFIX), workspace
    assert set(touched) <= {"wp-config.php"}, touched
    assert spies == {"write_archive": [], "collect_site_root": [], "verify": []}
    assert fake_database.calls == []
    assert len(fake_database.checks) == 1
    *lines, trailer = printed.out.splitlines()
    assert trailer == backup_cli.DRY_RUN_TRAILER
    assert "write-time failures" in trailer
    assert lines[0] == (
        f"1. resolve credentials from chain(wp-config:{site / 'wp-config.php'})"
    )
    assert lines[1] == "2. check dump readiness"
    assert lines[-1] == f"{len(lines)}. publish to file:{out / 'a'}"
    phases = [e for e in logs if e["event"] == "phase_completed"]
    assert [e["phase"] for e in phases] == [
        "DUMP", "COLLECT_SITE_ROOT", "WRITE_ARCHIVE", "VERIFY", "PUBLISH",
    ]  # fmt: skip
    assert {(e["outcome"], e["bytes"]) for e in phases} == {("skipped_dry_run", 0)}


def test_the_same_run_for_real_trips_every_spy(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    spies: dict[str, list[object]],
    opened: list[str],
) -> None:
    """The spies' teeth: without ``--whatIf`` each one fires."""
    final = tmp_path / "real.tar.gz"
    opened.clear()
    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) == 0
    assert all(spies.values()), spies
    assert "index.php" in opened
    assert fake_database.calls
    assert final.is_file()


@pytest.mark.parametrize("spelling", ["--whatIf", "--what-if"])
def test_what_if_after_backup_is_a_dry_run(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    spies: dict[str, list[object]],
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    """UX-1 (BL-E002-101): the flag where operators put it, after ``backup``."""
    final = tmp_path / "never.tar.gz"
    argv = ["backup", "--wp-dir", str(site), "--archive", str(final), spelling]
    code = main(argv)
    printed = capsys.readouterr()
    assert code == 0, printed.err
    assert not final.exists()
    assert spies == {"write_archive": [], "collect_site_root": [], "verify": []}
    assert fake_database.calls == []
    assert printed.out.splitlines()[-1] == backup_cli.DRY_RUN_TRAILER


def test_backup_help_lists_what_if(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["backup", "--help"])
    assert "--whatIf" in capsys.readouterr().out


def _direct_calls(path: Path, names: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in names:
                found.append(f"{path.name}:{node.lineno} {name}")
    return found


def _adapter_modules() -> set[str]:
    """Every module under ``adapters/``, discovered by walking it."""
    root = Path(adapters_package.__file__).parent
    found = {
        f"{ADAPTERS}.{path.relative_to(root).with_suffix('').as_posix()}".replace(
            "/", "."
        )
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
    }
    assert found, f"no adapter module under {root}"
    return found


def forbidden_imports(tree: ast.Module, modules: set[str]) -> list[str]:
    """Imports of adapter code outside :data:`ADAPTER_ALLOWANCE`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            taken = [(alias.name, None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            taken = [
                (f"{node.module}.{alias.name}", None)
                if f"{node.module}.{alias.name}" in modules
                else (node.module, alias.name)
                for alias in node.names
            ]
        else:
            continue
        for module, name in taken:
            if module != ADAPTERS and not module.startswith(f"{ADAPTERS}."):
                continue
            allowed = ADAPTER_ALLOWANCE.get(module)
            if allowed is None or name is None or name not in allowed:
                found.append(
                    f"{node.lineno} {module}{'' if name is None else ':' + name}"
                )
    return found


def forbidden_os_uses(tree: ast.Module) -> list[str]:
    return [
        f"{node.lineno} os.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr not in OS_ALLOWANCE
    ]


def _dry_run_mentions(node: ast.AST) -> list[int]:
    lines = []
    for inner in ast.walk(node):
        spelled = (
            inner.arg
            if isinstance(inner, ast.arg | ast.keyword)
            else inner.id
            if isinstance(inner, ast.Name)
            else inner.attr
            if isinstance(inner, ast.Attribute)
            else None
        )
        if spelled in DRY_RUN_NAMES:
            lines.append(getattr(inner, "lineno", 0))
    return lines


def definitions(tree: ast.Module) -> list[ast.stmt]:
    """Every top-level function and class the module defines."""
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    assert found, "the module defines nothing"
    return found


def executor_dry_run_mentions(tree: ast.Module) -> list[str]:
    """Names of ``dry_run``/``what_if`` outside the selection definitions."""
    return [
        f"{line} {node.name}"
        for node in definitions(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name not in SELECTION
        for line in _dry_run_mentions(node)
    ]


def _backup_tree() -> ast.Module:
    return ast.parse(Path(backup_cli.__file__).read_text(encoding="utf-8"))


def test_the_allowance_names_only_exported_adapter_names() -> None:
    modules = _adapter_modules()
    assert set(ADAPTER_ALLOWANCE) <= modules
    for module, names in ADAPTER_ALLOWANCE.items():
        exported = set(importlib.import_module(module).__all__)
        assert names <= exported, (module, sorted(names - exported))
    # The command implementation is not a query, whatever the allowance says.
    assert "WpDatabase" not in ADAPTER_ALLOWANCE[f"{ADAPTERS}.database"]


def test_the_root_imports_no_writer_dump_or_store() -> None:
    tree = _backup_tree()
    imported = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(f"{ADAPTERS}.")
    ]
    assert imported, "the scope is empty: cli/backup.py imports no adapter"
    assert forbidden_imports(tree, _adapter_modules()) == []


def test_the_root_uses_os_only_for_the_environment() -> None:
    assert forbidden_os_uses(_backup_tree()) == []


def test_no_executor_definition_names_the_dry_run() -> None:
    tree = _backup_tree()
    names = {getattr(node, "name", "") for node in definitions(tree)}
    assert {"execute_backup_plan", "backup"} <= names
    assert names >= SELECTION, sorted(SELECTION - names)
    assert executor_dry_run_mentions(tree) == []


PLANTED_ROOT = """\
import os
from l3io.wp.backup.adapters.tar import write_archive
from l3io.wp.backup.adapters import store
from l3io.wp.backup.adapters.database import WpDatabase, locate_wp_config
from l3io.wp.backup.adapters.phase import phase
import l3io.wp.backup.adapters.crypto


def execute_backup_plan(plan, *, dry_run):
    os.replace(plan.a, plan.b)
    return os.environ


def helper(request):
    if request.what_if:
        pass


def backup(site):
    return execute_backup_plan(site, dry_run=False)


def drive_backup(request):
    return request.dry_run
"""


def test_the_root_checks_see_planted_violations() -> None:
    tree = ast.parse(PLANTED_ROOT)
    assert forbidden_imports(tree, _adapter_modules()) == [
        f"2 {ADAPTERS}.tar:write_archive",
        f"3 {ADAPTERS}.store",
        f"4 {ADAPTERS}.database:WpDatabase",
        f"6 {ADAPTERS}.crypto",
    ]
    assert forbidden_os_uses(tree) == ["10 os.replace"]
    assert executor_dry_run_mentions(tree) == [
        "9 execute_backup_plan",
        "15 helper",
        "20 backup",
    ]


def test_the_root_calls_no_writer_or_traversal_directly() -> None:
    assert _direct_calls(Path(backup_cli.__file__), EFFECTS) == []


def test_the_ast_check_sees_a_direct_call(tmp_path: Path) -> None:
    planted = tmp_path / "root.py"
    planted.write_text(
        "def f(t):\n    t.collect_site_root(1)\n    write_archive(2)\n",
        encoding="utf-8",
    )
    assert _direct_calls(planted, EFFECTS) == [
        "root.py:2 collect_site_root",
        "root.py:3 write_archive",
    ]


@pytest.mark.parametrize("level", ["INFO", "WARNING", "ERROR"])
def test_a_refused_dry_run_still_prints_its_plan(
    site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
    level: str,
) -> None:
    final = tmp_path / "taken.tar.gz"
    final.write_bytes(b"an archive already here")
    argv = ["--log-level", level, "--whatIf", "backup", "--wp-dir", str(site)]
    code = main([*argv, "--archive", str(final)])
    printed = capsys.readouterr()
    assert code == ExitCode.REFUSED, printed.err
    lines = printed.out.splitlines()
    assert lines[0] == (
        f"1. resolve credentials from chain(wp-config:{site / 'wp-config.php'})"
    )
    assert lines[-1] == f"{len(lines)}. publish to file:{final}"
    assert final.read_bytes() == b"an archive already here"
    assert fake_database.calls == []


def test_a_refused_real_run_prints_nothing_on_stdout(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    final = tmp_path / "taken.tar.gz"
    final.write_bytes(b"an archive already here")
    code = main(["backup", "--wp-dir", str(site), "--archive", str(final)])
    printed = capsys.readouterr()
    assert code == ExitCode.REFUSED, printed.err
    assert printed.out == ""
