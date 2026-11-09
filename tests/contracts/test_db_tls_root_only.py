"""Owner decision 56 (amending BL-E003-064, L-6) — only the run roots choose TLS.

``db_tls`` (``--db-tls``/``--db-tls-ca``, their deprecated ``--admin-tls*``
spellings, or ``api.backup``'s and ``api.restore``'s argument) sets the TLS
of every database connection a backup or a restore makes; without it none is
required, as WordPress connects. The verify and doctor roots must never pass
one. This test finds every call that passes the ``db_tls=`` keyword and every
``DbTls(...)`` construction, and asserts each is in a function on
:data:`ALLOWED`: the backup and restore roots and drivers (``cli/backup.py``,
``cli/restore.py``, ``api/operations.py``), the CLI's one builder from the
flags (``cli/credentials.py`` ``db_tls_setting``) and the one plumbing step
that forwards the value into the database adapter (``cli/adapter_sets.py``
``real_adapters``).

**Scope, derived.** Every ``*.py`` under the package root, found by walking
it; a call is recognised by its callee's last name (bare, aliased by import,
or module-qualified), and a construction by the name ``DbTls`` or any name
an ``import ... as`` binds to it. An unused ``ALLOWED`` entry fails as stale.

**Not covered:** a keyword spread from a mapping (``f(**{"db_tls": x})``) is
caught only when the mapping is a dict literal written at the call; a value
passed positionally is not seen; a ``field(default_factory=DbTls)`` names the
class without calling it.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
KEYWORD = "db_tls"
TYPE = "DbTls"

#: ``<module path>::<function qualname>`` allowed to pass or build an DbTls.
ALLOWED = frozenset(
    {
        "cli/backup.py::run_backup",
        "cli/backup.py::drive_backup",
        "cli/restore.py::run_restore",
        "cli/restore.py::drive_restore",
        "cli/credentials.py::db_tls_setting",
        "api/operations.py::backup",
        "api/operations.py::restore",
        "cli/adapter_sets.py::real_adapters",
    }
)


def _type_names(tree: ast.Module) -> set[str]:
    names = {TYPE}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == TYPE and a.asname)
    return names


def _passes_admin_tls(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == KEYWORD:
            return True
        if (
            keyword.arg is None
            and isinstance(keyword.value, ast.Dict)
            and any(
                isinstance(k, ast.Constant) and k.value == KEYWORD
                for k in keyword.value.keys
            )
        ):
            return True
    return False


def _builds(call: ast.Call, names: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in names
    return isinstance(func, ast.Attribute) and func.attr == TYPE


def _module_sites(tree: ast.Module, relative: str) -> set[str]:
    names = _type_names(tree)
    found: set[str] = set()

    def visit(node: ast.AST, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, f"{qualname}.{child.name}".lstrip("."))
                continue
            if isinstance(child, ast.Call) and (
                _passes_admin_tls(child) or _builds(child, names)
            ):
                found.add(f"{relative}::{qualname or '<module>'}")
            visit(child, qualname)

    visit(tree, "")
    return found


def sites(root: Path) -> set[str]:
    """Each function that passes ``db_tls=`` or builds an ``DbTls``."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found |= _module_sites(tree, path.relative_to(root).as_posix())
    return found


def test_the_walk_finds_the_restore_roots() -> None:
    """Scope assertion: the known roots are seen, so the walk reaches them."""
    found = sites(PACKAGE)
    assert {"cli/restore.py::run_restore", "api/operations.py::restore"} <= found


def test_only_the_run_roots_pass_db_tls() -> None:
    assert sites(PACKAGE) - ALLOWED == set()


def test_no_allowed_site_is_stale() -> None:
    assert ALLOWED - sites(PACKAGE) == set()


def _plant(root: Path, relative: str, source: str) -> set[str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return sites(root)


def test_a_backup_root_passing_it_in_a_nested_module_is_found(tmp_path: Path) -> None:
    found = _plant(
        tmp_path,
        "cli/deep/backup2.py",
        "def run_backup(tls):\n    return real_adapters(source, db_tls=tls)\n",
    )
    assert found == {"cli/deep/backup2.py::run_backup"}


def test_an_aliased_construction_is_found(tmp_path: Path) -> None:
    found = _plant(
        tmp_path,
        "cli/verify.py",
        "from l3io.wp.backup.ports.database import DbTls as T\n"
        "class V:\n    def run(self):\n        return T(mode=None, ca_file=None)\n",
    )
    assert found == {"cli/verify.py::V.run"}


def test_a_qualified_construction_and_a_dict_spread_are_found(tmp_path: Path) -> None:
    found = _plant(
        tmp_path,
        "cli/doctor.py",
        "from l3io.wp.backup.ports import database\n"
        "def a():\n    database.DbTls(mode='verify-ca', ca_file=None)\n"
        "def b(tls):\n    real_adapters(**{'db_tls': tls})\n",
    )
    assert found == {"cli/doctor.py::a", "cli/doctor.py::b"}
