"""BL-E003-064 (L-6) — only the restore roots choose the administrator's TLS.

``admin_tls`` (``--admin-tls``/``--admin-tls-ca``, or ``api.restore``'s
argument) raises the TLS policy of every database connection a restore makes.
The backup, verify and doctor roots must never pass one: their connections stay
on ``tls_required_unless_local``. This test finds every call that passes the
``admin_tls=`` keyword and every ``AdminTls(...)`` construction, and asserts
each is in a function on :data:`ALLOWED`: the two restore roots
(``cli/restore.py`` ``run_restore`` and ``drive_restore``, ``api/operations.py``
``restore``) and the one plumbing step that forwards the root's value into the
database adapter (``cli/adapter_sets.py`` ``real_adapters``).

**Scope, derived.** Every ``*.py`` under the package root, found by walking
it; a call is recognised by its callee's last name (bare, aliased by import,
or module-qualified), and a construction by the name ``AdminTls`` or any name
an ``import ... as`` binds to it. An unused ``ALLOWED`` entry fails as stale.

**Not covered:** a keyword spread from a mapping (``f(**{"admin_tls": x})``) is
caught only when the mapping is a dict literal written at the call; a value
passed positionally is not seen.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
KEYWORD = "admin_tls"
TYPE = "AdminTls"

#: ``<module path>::<function qualname>`` allowed to pass or build an AdminTls.
ALLOWED = frozenset(
    {
        "cli/restore.py::run_restore",
        "cli/restore.py::drive_restore",
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
    """Each function that passes ``admin_tls=`` or builds an ``AdminTls``."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found |= _module_sites(tree, path.relative_to(root).as_posix())
    return found


def test_the_walk_finds_the_restore_roots() -> None:
    """Scope assertion: the known roots are seen, so the walk reaches them."""
    found = sites(PACKAGE)
    assert {"cli/restore.py::run_restore", "api/operations.py::restore"} <= found


def test_only_the_restore_roots_pass_admin_tls() -> None:
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
        "def run_backup(tls):\n    return real_adapters(source, admin_tls=tls)\n",
    )
    assert found == {"cli/deep/backup2.py::run_backup"}


def test_an_aliased_construction_is_found(tmp_path: Path) -> None:
    found = _plant(
        tmp_path,
        "cli/verify.py",
        "from l3io.wp.backup.adapters.database import AdminTls as T\n"
        "class V:\n    def run(self):\n        return T(mode=None, ca_file=None)\n",
    )
    assert found == {"cli/verify.py::V.run"}


def test_a_qualified_construction_and_a_dict_spread_are_found(tmp_path: Path) -> None:
    found = _plant(
        tmp_path,
        "cli/doctor.py",
        "from l3io.wp.backup.adapters import database\n"
        "def a():\n    database.AdminTls(mode='verify-ca', ca_file=None)\n"
        "def b(tls):\n    real_adapters(**{'admin_tls': tls})\n",
    )
    assert found == {"cli/doctor.py::a", "cli/doctor.py::b"}
