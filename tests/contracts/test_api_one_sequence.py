"""AD-16 — the library does not re-sequence what the drivers do.

No module under ``api/`` (found on disk, never listed) names a
``RestorePhase``, imports from ``core.restore``, or calls ``open_run``,
``join_or_open``, ``bind_adapters``, ``real_adapters``, ``dry_run_adapters``
or a Site Lock's ``acquire``: the drivers do all of those, for the CLI and the
library alike. A violation planted deep in a copy of the tree is reported.

One import from ``core.restore`` is not sequencing: an exception class
``api/errors.py`` re-exports (AD-14, E006-S01-002). A name imported from there
is allowed exactly when it resolves to a ``BackupError`` subclass.
"""

from __future__ import annotations

import ast
import importlib
import shutil
from pathlib import Path

import pytest

from l3io.wp.backup.api import operations
from l3io.wp.backup.domain.errors import BackupError
from tests.qualified_names import ImportTable, module_package

API = Path(operations.__file__).parent
API_PACKAGE = "l3io.wp.backup.api"

FORBIDDEN_CALLS = frozenset(
    {
        "open_run",
        "join_or_open",
        "bind_adapters",
        "real_adapters",
        "dry_run_adapters",
        "acquire",
    }
)
FORBIDDEN_NAMES = frozenset({"RestorePhase"})
FORBIDDEN_MODULES = ("l3io.wp.backup.core.restore",)


def _last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _is_error_class(name: str) -> bool:
    """``name`` (``module.Attr``) resolves to a ``BackupError`` subclass."""
    module, _, attr = name.rpartition(".")
    try:
        obj = getattr(importlib.import_module(module), attr, None)
    except ImportError:
        return False
    return isinstance(obj, type) and issubclass(obj, BackupError)


def _violations(directory: Path, *, root_package: str = API_PACKAGE) -> list[str]:
    """Matched on the *qualified* name, so an alias or a from-import is seen."""
    found: set[str] = set()
    for path in sorted(directory.rglob("*.py")):
        where = path.relative_to(directory)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = module_package(path, root=directory, root_package=root_package)
        table = ImportTable(tree, package=package)
        found |= {
            f"{where}:{line}: calls {_last(name)}"
            for line, name in table.calls(tree)
            if _last(name) in FORBIDDEN_CALLS
        }
        found |= {
            f"{where}:{line}: names {_last(name)}"
            for line, name in table.references(tree)
            if _last(name) in FORBIDDEN_NAMES
        }
        found |= {
            f"{where}:{line}: imports {name}"
            for line, name in table.imported
            if (name.startswith(FORBIDDEN_MODULES) and not _is_error_class(name))
            or _last(name) in FORBIDDEN_NAMES
        }
    return sorted(found)


def test_the_scope_holds_the_entry_points() -> None:
    assert (API / "operations.py") in set(API.rglob("*.py"))


def test_api_sequences_nothing_the_drivers_do() -> None:
    assert _violations(API) == []


def test_a_planted_resequencing_is_reported(tmp_path: Path) -> None:
    copy = tmp_path / "api"
    shutil.copytree(API, copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert _violations(copy) == []
    deep = copy / "nested" / "deeper"
    deep.mkdir(parents=True)
    (deep / "helper.py").write_text(
        "from l3io.wp.backup.core.restore import build_restore_plan\n"
        "from l3io.wp.backup.core.restore import TargetChangedError\n"
        "def f(ctx, lock):\n"
        "    with join_or_open(None, operation='x'):\n"
        "        ctx.bind_adapters(real_adapters())\n"
        "        lock.acquire(root, holder)\n"
        "        return RestorePhase.EXTRACT\n",
        encoding="utf-8",
    )
    with (copy / "operations.py").open("a", encoding="utf-8") as handle:
        handle.write("\ndry_run_adapters(None)\n")
    found = _violations(copy)
    assert sorted(line.split(": ", 1)[1] for line in found) == [
        "calls acquire",
        "calls bind_adapters",
        "calls dry_run_adapters",
        "calls join_or_open",
        "calls real_adapters",
        "imports l3io.wp.backup.core.restore.build_restore_plan",
        "names RestorePhase",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from l3io.wp.backup.core import restore as r\nr.build_restore_plan()\n",
            "imports l3io.wp.backup.core.restore",
        ),
        ("from ..core import restore\n", "imports l3io.wp.backup.core.restore"),
        (
            "import l3io.wp.backup.core.restore.x\n",
            "imports l3io.wp.backup.core.restore.x",
        ),
        ("x = restore_mod.RestorePhase.EXTRACT\n", "names RestorePhase"),
        (
            "from l3io.wp.backup.domain.restore import RestorePhase as P\nP.EXTRACT\n",
            "names RestorePhase",
        ),
        (
            "from l3io.wp.backup.cli.context import join_or_open as j\nj(None)\n",
            "calls join_or_open",
        ),
        (
            "import l3io.wp.backup.cli.adapter_sets as s\ns.real_adapters()\n",
            "calls real_adapters",
        ),
    ],
    ids=[
        "from-alias",
        "relative",
        "dotted",
        "attribute",
        "aliased-name",
        "aliased-call",
        "module-alias",
    ],
)
def test_every_import_spelling_is_resolved(
    tmp_path: Path, source: str, expected: str
) -> None:
    (tmp_path / "planted.py").write_text(source, encoding="utf-8")
    found = [line.split(": ", 1)[1] for line in _violations(tmp_path)]
    assert expected in found
