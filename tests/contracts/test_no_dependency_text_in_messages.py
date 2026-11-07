"""AD-7, AD-14, FR-23 — a package exception's message never carries a caught one's text.

Every message is built from structured fields (E006-S01-002, Security
considerations): a dependency's text can echo a value it was handed, so it
is never forwarded. An ``ast`` walk over every ``*.py`` under
``src/l3io/wp/backup/`` flags ``str(e)``, ``repr(e)``, ``format(e)`` or an
f-string ``{e}`` where ``e`` is

- the name an ``except ... as e`` clause binds, when that clause's body
  contains a ``raise``; or
- a function parameter annotated as an exception (its annotation names
  ``Exception``, ``Error`` or ``BaseException``), when the function calls
  nothing named ``redact``: a helper that turns an exception into a reason.

Attributes are allowed (``e.strerror``, ``e.kind``, ``type(e).__name__``):
they are fields. **What this cannot see:** text taken from an exception
through any other spelling (``e.args[0]``, ``"%s" % e``, a helper whose
parameter is unannotated), and an exception formatted where text leaves the
program and is redacted there (``cli/exit.py::render_failure``, the tracer's
span status), which ADR-0018 places outside this rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.exception_scope import PACKAGE_DIR

_TEXT_CALLS = frozenset({"str", "repr", "format"})
_EXCEPTION_WORDS = ("Exception", "Error")


def _uses_text(body: list[ast.stmt], name: str) -> list[int]:
    lines = []
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _TEXT_CALLS
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == name
            ) or (
                isinstance(node, ast.FormattedValue)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
            ):
                lines.append(node.lineno)
    return lines


def _raises(body: list[ast.stmt]) -> bool:
    return any(isinstance(n, ast.Raise) for s in body for n in ast.walk(s))


def _redacts(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else None
                if isinstance(func, ast.Attribute):
                    called = func.attr
                if called == "redact":
                    return True
    return False


def violations(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        where = path.relative_to(root)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler) and node.name and _raises(node.body):
                found += [f"{where}:{n}" for n in _uses_text(node.body, node.name)]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if _redacts(node.body):
                    continue
                for arg in [*node.args.args, *node.args.kwonlyargs]:
                    annotation = ast.unparse(arg.annotation) if arg.annotation else ""
                    if any(word in annotation for word in _EXCEPTION_WORDS):
                        found += [
                            f"{where}:{n}" for n in _uses_text(node.body, arg.arg)
                        ]
    return sorted(set(found))


def test_the_walk_reaches_every_layer() -> None:
    files = list(PACKAGE_DIR.rglob("*.py"))
    for layer in ("domain", "adapters", "cli", "core", "api"):
        assert any(f.parent.name == layer for f in files), layer


def test_no_package_message_interpolates_a_caught_exception() -> None:
    assert violations(PACKAGE_DIR) == []


def test_planted_violations_are_reported(tmp_path: Path) -> None:
    """Each spelling, hidden in a nested directory with no ``__init__.py``."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "planted.py").write_text(
        "def one():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        raise Planted(str(exc)) from exc\n"  # 5
        "def two():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as err:\n"
        "        msg = f'failed: {err}'\n"  # 10
        "        raise Planted(msg) from err\n"
        "def helper(exc: BaseException) -> str:\n"
        "    return repr(exc)\n"  # 13
        "def allowed(exc: OSError) -> str:\n"
        "    return exc.strerror or type(exc).__name__\n"
        "def rendered(exc: BaseException, redact) -> str:\n"
        "    return redact(str(exc))\n"
        "def logged():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        log(str(exc))\n",
        encoding="utf-8",
    )
    assert violations(tmp_path) == [
        "a/b/planted.py:10",
        "a/b/planted.py:13",
        "a/b/planted.py:5",
    ]
