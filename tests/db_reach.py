"""Which collected tests need a database: a pytest plugin, not a test module.

``tests/contracts/test_integration_marker_reach.py`` loads it in a child pytest
(``python -m pytest --collect-only -p tests.db_reach``) and compares two
collections, one without and one with ``DB_HOST``. The database legs of
``ci.yml`` run only ``-m integration`` and the no-database legs only
``-m "not integration"``, so a test that needs the database and is not marked
``integration`` runs nowhere -- it skips in the one tier and is deselected in
the other. That contract test is what stops it.

For every collected item this writes one JSON line to ``$L3IO_DB_REACH_OUT``:

``nodeid``
    the item.
``integration``
    whether it carries the marker (its own, its class's or its module's).
``skip``
    the reason its ``skip``/``skipif`` marks skip it in THIS environment, or
    ``null`` -- the contract test diffs this between the two collections, so a
    gate on ``DB_HOST`` is found whatever its condition is spelled as (a string,
    a boolean, a module-level ``pytestmark``, a shared ``live = skipif(...)``).
``reads``
    the functions in the item's reach that read the CI database's environment
    (``DB_HOST``/``DB_PASSWORD``) as a requirement: the test function, every
    fixture in its closure, and every function defined under the test tree that
    those call, followed through the callee's module globals. A read is
    ``os.environ["DB_HOST"]``, ``os.environ.get("DB_HOST")`` or
    ``os.getenv("DB_HOST")`` with no default, or ``"DB_HOST" (not) in
    os.environ``. A ``.get`` WITH a default is not a requirement -- the test runs
    against the default (``write_wp_config`` writes ``localhost``).
``clients``
    database client modules the item's module imports at module level
    (``import X`` or ``pytest.importorskip("X")``). The client set is DERIVED:
    a module imported, inside any function in the item's reach or in any test
    module's top level, by a function that also reads the database environment
    (``live_account`` imports ``pymysql``). Never a hand-kept list.

Coverage, stated: a call through a variable, a ``getattr`` or a method on an
instance is not followed; a database reached with no environment read at all
(a hardcoded host) is not seen. Tool probes (``shutil.which("wp")``) are not a
database signal; ``tests/integration/test_search_replace.py`` fails rather than
skips on a missing tool when ``CI`` is set.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

#: The CI database's environment, as ``ci.yml``'s database legs set it.
DB_ENV = frozenset({"DB_HOST", "DB_PASSWORD"})
OUT_ENV = "L3IO_DB_REACH_OUT"


def _is_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _db_key(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value in DB_ENV


def reads_db_env(tree: ast.AST) -> bool:
    """True when ``tree`` requires the database environment to be set."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)
            and _db_key(node.slice)
        ):
            return True
        if isinstance(node, ast.Call) and node.args and _db_key(node.args[0]):
            func = node.func
            no_default = len(node.args) == 1 and not node.keywords
            getter = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_environ(func.value)
            )
            getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                isinstance(func, ast.Name) and func.id == "getenv"
            )
            if (getter or getenv) and no_default:
                return True
        if (
            isinstance(node, ast.Compare)
            and _db_key(node.left)
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
            and any(_is_environ(c) for c in node.comparators)
        ):
            return True
    return False


def imported_modules(statements: list[ast.stmt] | ast.AST) -> set[str]:
    """Top-level module names imported by ``import``/``from``/``importorskip``."""
    nodes = statements if isinstance(statements, list) else [statements]
    found: set[str] = set()
    for stmt in nodes:
        # A module's top level: the statements themselves, not nested defs.
        walk = (
            [stmt]
            if isinstance(statements, list)
            and isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else list(ast.walk(stmt))
        )
        for node in walk:
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module.split(".")[0])
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "importorskip"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


class _Reach:
    """Function-level analysis, memoised across items."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots]
        self._files: dict[str, dict[int, ast.AST]] = {}
        self._reads: dict[types.CodeType, list[str]] = {}
        self._scope: dict[str, bool] = {}
        #: Client modules imported by a function that also reads the DB env.
        self.clients: set[str] = set()

    def in_scope(self, func: object) -> bool:
        code = getattr(func, "__code__", None)
        if code is None:
            return False
        name = code.co_filename
        if name not in self._scope:
            path = Path(name).resolve()
            self._scope[name] = any(path.is_relative_to(r) for r in self.roots)
        return self._scope[name]

    def tree(self, func: types.FunctionType) -> ast.AST | None:
        """The function's definition node, from its file parsed once."""
        code = func.__code__
        if code.co_filename not in self._files:
            index: dict[int, ast.AST] = {}
            try:
                text = Path(code.co_filename).read_text(encoding="utf-8")
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # co_firstlineno is the first decorator's line, if any.
                        lines = [node.lineno, *(d.lineno for d in node.decorator_list)]
                        index[min(lines)] = node
            except (OSError, SyntaxError, UnicodeDecodeError):
                pass
            self._files[code.co_filename] = index
        return self._files[code.co_filename].get(code.co_firstlineno)

    def _callees(self, func: types.FunctionType, tree: ast.AST) -> Iterator[Any]:
        scope = dict(func.__globals__)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain: list[str] = []
            target: ast.expr = node.func
            while isinstance(target, ast.Attribute):
                chain.insert(0, target.attr)
                target = target.value
            if not isinstance(target, ast.Name) or target.id not in scope:
                continue
            obj: Any = scope[target.id]
            for attr in chain:
                obj = getattr(obj, attr, None)
            yield obj

    def reads(self, func: object) -> list[str]:
        """Qualified names of in-scope functions reachable from ``func`` that
        read the database environment (``func`` itself included)."""
        func = inspect.unwrap(func)  # type: ignore[arg-type]
        if not isinstance(func, types.FunctionType) or not self.in_scope(func):
            return []
        code = func.__code__
        if code in self._reads:
            return self._reads[code]
        self._reads[code] = []  # cycle guard
        tree = self.tree(func)
        if tree is None:
            return []
        found: list[str] = []
        if reads_db_env(tree):
            found.append(f"{func.__module__}.{func.__qualname__}")
            self.clients |= imported_modules(tree)
        for callee in self._callees(func, tree):
            for name in self.reads(callee):
                if name not in found:
                    found.append(name)
        self._reads[code] = found
        return found


def _item_functions(item: pytest.Item) -> Iterator[object]:
    function = getattr(item, "function", None)
    if function is not None:
        yield function
    info = getattr(item, "_fixtureinfo", None)
    for name in getattr(item, "fixturenames", ()):
        for fixturedef in info.name2fixturedefs.get(name, ()) if info else ():
            yield fixturedef.func


def _module_clients(item: pytest.Item, cache: dict[str, set[str]]) -> set[str]:
    module = getattr(item, "module", None)
    path = getattr(module, "__file__", None)
    if path is None:
        return set()
    if path not in cache:
        try:
            body = ast.parse(Path(path).read_text(encoding="utf-8")).body
        except (OSError, SyntaxError):
            body = []
        cache[path] = imported_modules(body)
    return cache[path]


def pytest_collection_finish(session: pytest.Session) -> None:
    from _pytest.skipping import evaluate_skip_marks

    out = os.environ.get(OUT_ENV)
    if not out:
        return
    config = session.config
    roots = [Path(__file__).resolve().parent]
    roots += [Path(a.split("::")[0]) for a in config.args]
    reach = _Reach([r if r.is_dir() else r.parent for r in roots])
    module_imports: dict[str, set[str]] = {}
    rows: list[dict[str, Any]] = []
    for item in session.items:
        reads: list[str] = []
        for func in _item_functions(item):
            for name in reach.reads(func):
                if name not in reads:
                    reads.append(name)
        skip = evaluate_skip_marks(item)
        rows.append(
            {
                "nodeid": item.nodeid,
                "integration": item.get_closest_marker("integration") is not None,
                "skip": None if skip is None else skip.reason,
                "reads": reads,
                "module_imports": sorted(_module_clients(item, module_imports)),
            }
        )
    # Every reader in any module, so the client set does not depend on which
    # items happen to call one.
    for module in list(sys.modules.values()):
        for value in list(getattr(module, "__dict__", {}).values()):
            if isinstance(value, types.FunctionType) and reach.in_scope(value):
                reach.reads(value)
    # Third-party clients only: the standard library, pytest, the product's own
    # namespace and the test tree are imported by everything.
    first_party = {"pytest", "_pytest", "tests", __name__.split(".")[0], "l3io"}
    clients = sorted(
        m
        for m in reach.clients
        if m not in sys.stdlib_module_names and m not in first_party
    )
    for row in rows:
        row["clients"] = sorted(set(row.pop("module_imports")) & set(clients))
    with Path(out).open("w", encoding="utf-8") as fh:
        json.dump({"clients": clients, "items": rows}, fh)
