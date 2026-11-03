"""AD-18.3/ADR-0015/ADR-0024 — loading over a non-empty database cannot be expressed.

Two halves:

* **The ports.** No method named ``load*`` on any ``Protocol`` under ``ports/``
  declares a ``replace``, ``identities``/``identity``, ``passphrase`` or
  credential parameter — by name (``*credential*``, ``*password*``) or by an
  annotation naming a ``Credential`` type.
* **The adapters.** Every call to database's ``load`` — reached through
  ``l3io.wp.database`` imported under any alias, or ``load`` imported from it
  by name — passes ``replace=None`` or omits it, and passes no ``**`` mapping
  that could carry one.

**Scope, derived.** The ports half parses every ``*.py`` under ``ports/``
(walked recursively, asserted non-empty, and asserted to reach
``DatabaseCommand.load_dump``); the adapters half parses every ``*.py`` under
the package, so a ``load`` call moved out of ``adapters/database.py`` is still
seen. Both are run over planted modules — in a nested directory, through an
alias, by a from-import, and through ``**kwargs`` — so the guards are shown to
see what they claim to.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
PORTS = PACKAGE / "ports"
DATABASE_MODULE = "l3io.wp.database"

FORBIDDEN_NAMES = frozenset({"replace", "identities", "identity", "passphrase"})
FORBIDDEN_FRAGMENTS = ("credential", "password")


def _trees(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_protocol(cls: ast.ClassDef) -> bool:
    """``Protocol``, ``typing.Protocol``, or either subscripted (generic)."""
    bases = [b.value if isinstance(b, ast.Subscript) else b for b in cls.bases]
    return any(
        (isinstance(b, ast.Name) and b.id == "Protocol")
        or (isinstance(b, ast.Attribute) and b.attr == "Protocol")
        for b in bases
    )


def _forbidden(arg: ast.arg) -> bool:
    name = arg.arg.lower()
    annotation = "" if arg.annotation is None else ast.unparse(arg.annotation)
    return (
        name in FORBIDDEN_NAMES
        or any(f in name for f in FORBIDDEN_FRAGMENTS)
        or "Credential" in annotation
    )


def load_methods(root: Path) -> dict[str, list[str]]:
    """``Protocol.load*`` -> its forbidden parameters, for every port module."""
    found: dict[str, list[str]] = {}
    for _, tree in _trees(root):
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef) or not _is_protocol(cls):
                continue
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("load"):
                    a = item.args
                    params = [*a.posonlyargs, *a.args, *a.kwonlyargs]
                    params += [p for p in (a.vararg, a.kwarg) if p is not None]
                    found[f"{cls.name}.{item.name}"] = [
                        p.arg for p in params if _forbidden(p)
                    ]
    return found


def _database_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to the database module, and names bound to its ``load``."""
    modules: set[str] = set()
    loads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == DATABASE_MODULE and alias.asname:
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full == DATABASE_MODULE:
                    modules.add(alias.asname or alias.name)
                elif node.module == DATABASE_MODULE and alias.name == "load":
                    loads.add(alias.asname or alias.name)
    return modules, loads


def _is_database_load(call: ast.Call, modules: set[str], loads: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in loads
    if isinstance(func, ast.Attribute) and func.attr == "load":
        value = func.value
        if isinstance(value, ast.Name):
            return value.id in modules
        # ``l3io.wp.database.load(...)`` spelled in full.
        return ast.unparse(value) == DATABASE_MODULE
    return False


def _replaces(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg is None:  # ``**mapping`` could carry ``replace``
            return True
        if keyword.arg == "replace":
            value = keyword.value
            if not (isinstance(value, ast.Constant) and value.value is None):
                return True
    return False


def load_calls(root: Path) -> tuple[int, list[str]]:
    """How many database ``load`` calls under ``root``, and the offending ones."""
    total, bad = 0, []
    for path, tree in _trees(root):
        modules, loads = _database_names(tree)
        if not modules and not loads:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_database_load(node, modules, loads):
                total += 1
                if _replaces(node):
                    bad.append(f"{path.relative_to(root)}:{node.lineno}")
    return total, bad


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------


def test_no_port_load_method_can_express_replace_or_a_secret() -> None:
    assert list(PORTS.rglob("*.py")), f"no port module under {PORTS}"
    found = load_methods(PORTS)
    assert "DatabaseCommand.load_dump" in found, "the walk missed load_dump"
    assert {k: v for k, v in found.items() if v} == {}


def test_no_database_load_call_passes_replace() -> None:
    assert (PACKAGE / "adapters" / "database.py").is_file()
    _, bad = load_calls(PACKAGE)
    assert bad == [], f"AD-18.3 — load(replace=...) at {bad}"


@pytest.mark.xfail(
    strict=True,
    reason="no database load call exists until E002-S02-009 implements load_dump; "
    "when this XPASSes, drop the marker so the check above is never vacuous",
)
def test_the_walk_reaches_a_database_load_call() -> None:
    total, _ = load_calls(PACKAGE)
    assert total >= 1


# --------------------------------------------------------------------------
# Scope attacks
# --------------------------------------------------------------------------


def test_a_planted_port_in_a_nested_directory_is_seen(tmp_path: Path) -> None:
    deep = tmp_path / "ports" / "deep"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text(
        "from typing import Protocol\n"
        "import typing\n"
        "class LoaderCommand(typing.Protocol):\n"
        "    def load_all(self, src, *, replace: bool) -> None: ...\n"
        "    def load_secret(self, src, admin: 'CredentialSource') -> None: ...\n"
        "    def load_key(self, src, *, passphrase, db_password) -> None: ...\n"
        "    def load_ok(self, src) -> None: ...\n"
        "class GenericCommand(Protocol[T]):\n"
        "    def load_into(self, src, *, replace: T) -> None: ...\n"
        "class NotAPort:\n"
        "    def load(self, replace: bool) -> None: ...\n",
        encoding="utf-8",
    )
    assert load_methods(tmp_path / "ports") == {
        "LoaderCommand.load_all": ["replace"],
        "LoaderCommand.load_secret": ["admin"],
        "LoaderCommand.load_key": ["passphrase", "db_password"],
        "LoaderCommand.load_ok": [],
        "GenericCommand.load_into": ["replace"],
    }


def test_a_planted_load_call_is_seen_through_every_spelling(tmp_path: Path) -> None:
    deep = tmp_path / "elsewhere" / "deeper"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text(
        "from l3io.wp import database as wpdb\n"
        "wpdb.load(c, s, src, replace=True)\n"
        "wpdb.load(c, s, src, replace=None)\n"
        "wpdb.load(c, s, src)\n",
        encoding="utf-8",
    )
    (deep / "b.py").write_text(
        "from l3io.wp.database import load as put\n"
        "put(c, s, src, replace='drop')\n"
        "import l3io.wp.database as db2\n"
        "db2.load(c, s, src, **options)\n"
        "other.load(c, s, src, replace=True)\n",
        encoding="utf-8",
    )
    total, bad = load_calls(tmp_path)
    assert total == 5
    assert bad == [
        "elsewhere/deeper/a.py:2",
        "elsewhere/deeper/b.py:2",
        "elsewhere/deeper/b.py:4",
    ]
