"""BL-E006-013 (owner decision 2026-11-07) — no driver reads ``os.environ``.

``drive_backup``, ``drive_restore`` and ``drive_verify`` take an ``environ``
mapping; the CLI root and the library pass ``os.environ``. The guard walks
every ``drive_*`` function in every ``cli/`` module (the scope is discovered,
not listed) and every module-level function it reaches by name in the same
module, and refuses ``os.environ``, ``os.getenv`` and ``os.environb``.

**Not covered:** a read through another module's function (the credential
builders take ``environ`` as an argument, which is the point), or through an
alias of ``os``. Review is the guard for those.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from l3io.wp.backup import cli

CLI_DIR = Path(cli.__file__).resolve().parent
FORBIDDEN = frozenset({"environ", "environb", "getenv"})


def _reads(source: str) -> list[str]:
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    todo = [name for name in functions if name.startswith("drive_")]
    reached: set[str] = set()
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        todo += [
            c.func.id
            for c in ast.walk(functions[name])
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id in functions
        ]
    return sorted(
        f"{name}:{node.lineno}"
        for name in reached
        for node in ast.walk(functions[name])
        if isinstance(node, ast.Attribute)
        and node.attr in FORBIDDEN
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _driver_modules() -> list[Path]:
    found = [
        path
        for path in sorted(CLI_DIR.glob("*.py"))
        if "\ndef drive_" in path.read_text(encoding="utf-8")
    ]
    names = {p.name for p in found}
    assert {"backup.py", "restore.py", "verify.py"} <= names, names
    return found


def test_no_driver_reads_os_environ() -> None:
    for path in _driver_modules():
        assert _reads(path.read_text(encoding="utf-8")) == [], path.name


def test_the_drivers_take_environ() -> None:
    from l3io.wp.backup.cli.backup import drive_backup
    from l3io.wp.backup.cli.restore import drive_restore
    from l3io.wp.backup.cli.verify import drive_verify

    for driver in (drive_backup, drive_restore, drive_verify):
        assert "environ" in inspect.signature(driver).parameters


def test_a_planted_read_in_a_driver_helper_is_found() -> None:
    planted = (
        "import os\n"
        "def drive_x():\n    helper()\n"
        "def helper():\n    return os.environ.get('X')\n"
        "def run_x():\n    return os.environ\n"
    )
    assert _reads(planted) == ["helper:5"]


def test_a_planted_read_in_the_real_backup_driver_is_found() -> None:
    source = (CLI_DIR / "backup.py").read_text(encoding="utf-8")
    planted = source.replace(
        "            environ=environ,\n", "            environ=os.environ,\n", 1
    )
    assert planted != source
    assert [r.split(":")[0] for r in _reads(planted)] == ["drive_backup"]
