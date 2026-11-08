"""SD-02-12 — every log event the package emits has a row in docs/logging.md.

**Scope, derived (global rule 4).** The emitted set is read from the source by
AST, never listed: every call ``<x>.debug/info/warning/error/critical/
exception/msg("<event>", ...)`` whose first argument is a ``lower_snake``
literal, anywhere under the package root; a call through a local name bound to
such a method (``emit = _log.info if ... else _log.error``); and a call to a
helper function that passes its own first parameter to such a method as the
event (``_completed``, ``emit_failure``), with those helpers found by the same
walk.

**What this does NOT cover:** an event name built at run time (an f-string or
a variable that is not a helper's first parameter), and records a dependency
logs. The catalogue's "When" text is checked by review, not here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "l3io" / "wp" / "backup"
LOGGING_MD = ROOT / "docs" / "logging.md"

LEVELS = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception", "msg"}
)
_EVENT = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_ROW = re.compile(r"^\| `([a-z0-9_]+)` \|")


def _level_attributes(expr: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr in LEVELS
        for node in ast.walk(expr)
    )


def _helpers(trees: list[ast.Module]) -> set[str]:
    """Functions that log their first (non-self) parameter as the event."""
    found: set[str] = set()
    for tree in trees:
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = [a.arg for a in func.args.posonlyargs + func.args.args]
            params = [p for p in params if p not in ("self", "cls")]
            if not params:
                continue
            for call in ast.walk(func):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in LEVELS
                    and call.args
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == params[0]
                ):
                    found.add(func.name)
    return found


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def emitted_events(root: Path) -> dict[str, list[str]]:
    """Each event name emitted under ``root``, with where it is emitted."""
    paths = sorted(root.rglob("*.py"))
    trees = [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in paths]
    helpers = _helpers(trees)
    events: dict[str, list[str]] = {}
    for path, tree in zip(paths, trees, strict=True):
        aliases = {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute | ast.IfExp)
            and _level_attributes(node.value)
        }
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                and _EVENT.fullmatch(call.args[0].value)
            ):
                continue
            name = _callee(call)
            via_level = isinstance(call.func, ast.Attribute) and name in LEVELS
            via_alias = isinstance(call.func, ast.Name) and name in aliases
            if via_level or via_alias or name in helpers:
                where = f"{path.relative_to(root)}:{call.lineno}"
                events.setdefault(call.args[0].value, []).append(where)
    return events


def documented_events(text: str) -> list[str]:
    """The event names in the ``## Events`` table of ``text``, in order."""
    section = text.split("\n## Events\n", 1)[1].split("\n## ", 1)[0]
    return [m.group(1) for line in section.splitlines() if (m := _ROW.match(line))]


EMITTED = emitted_events(PACKAGE)
DOCUMENTED = documented_events(LOGGING_MD.read_text(encoding="utf-8"))


def test_the_derived_scope_is_not_empty() -> None:
    """Without this, the tests below pass over an empty set."""
    assert {"run_started", "run_finished", "phase_completed", "command_failed"} <= set(
        EMITTED
    )
    assert {"database_surveyed", "dump_loaded"} <= set(EMITTED), "helper events"
    assert DOCUMENTED, "docs/logging.md has no ## Events rows"


def test_every_emitted_event_has_a_row() -> None:
    missing = {e: EMITTED[e] for e in sorted(set(EMITTED) - set(DOCUMENTED))}
    assert missing == {}


def test_every_row_names_an_emitted_event() -> None:
    assert sorted(set(DOCUMENTED) - set(EMITTED)) == []


def test_no_event_has_two_rows() -> None:
    assert sorted({e for e in DOCUMENTED if DOCUMENTED.count(e) > 1}) == []


def test_the_deriver_sees_a_planted_event_in_a_nested_module(tmp_path: Path) -> None:
    """Attack the scope: a new module, deep in a new sub-package, each form."""
    planted = tmp_path / "pkg" / "new" / "deeper" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "import structlog\n"
        "_log = structlog.get_logger()\n"
        "def _helper(event, **fields):\n"
        "    _log.warning(event, **fields)\n"
        "def run(ok):\n"
        '    _log.info("planted_event_xyz")\n'
        "    emit = _log.info if ok else _log.error\n"
        '    emit("planted_alias_event")\n'
        '    _helper("planted_helper_event")\n'
        '    print("not_an_event_call")\n',
        encoding="utf-8",
    )
    found = emitted_events(tmp_path / "pkg")
    assert set(found) == {
        "planted_event_xyz",
        "planted_alias_event",
        "planted_helper_event",
    }
    assert found["planted_event_xyz"] == ["new/deeper/planted.py:6"]


def test_the_row_parser_reads_only_the_events_section() -> None:
    text = (
        "## Before\n| `outside_one` | INFO | x |\n"
        "## Events\n| Event | Level | When |\n|---|---|---|\n"
        "| `inside_one` | INFO | x |\n### Sub\nprose\n"
        "## After\n| `outside_two` | INFO | x |\n"
    )
    assert documented_events(text) == ["inside_one"]
