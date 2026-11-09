"""BL-E002-092 — a SIGTERM (``RunTerminated``) is never swallowed by a broad handler.

``RunTerminated`` is a ``BackupError``, so an ``except Exception`` catches it.
Before the fix the tracer guard disabled tracing and carried on, the exclusion
decision turned it into an ``ArchiveWriteError``, the cutover read the key back
and went on to the next one, and SNAPSHOT wrapped it as a snapshot failure.

**Rule.** In every module of the package, a handler that catches ``Exception``
must follow, in the same ``try``, a handler for ``RunTerminated`` (or
``BackupError``) whose body is a bare ``raise``. A handler whose whole purpose
is to map every failure — the CLI's ``main`` and the API boundary — says so on
its ``except`` line with a comment holding ``RunTerminated: mapped here``.

**Scope, derived.** Every ``*.py`` under the package root, found by walking it.
A bare ``except:`` and ``except BaseException`` are out of scope: in this
package they clean up and re-raise.
"""

from __future__ import annotations

import ast
import pathlib
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.adapters.noop import GuardedTracer
from l3io.wp.backup.adapters.tar import _decide
from l3io.wp.backup.domain.errors import RunTerminated
from l3io.wp.backup.ports.exclusion import ExclusionDecision

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
MARKER = "RunTerminated: mapped here"
_PASS_THROUGH = {"RunTerminated", "BackupError"}


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    items = node.elts if isinstance(node, ast.Tuple) else [node]
    found: set[str] = set()
    for item in items:
        if isinstance(item, ast.Name):
            found.add(item.id)
        elif isinstance(item, ast.Attribute):
            found.add(item.attr)
    return found


def _re_raises(handler: ast.ExceptHandler) -> bool:
    body = handler.body
    return len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is None


def violations(source: str, label: str) -> list[str]:
    """Every ``except Exception`` that would swallow ``RunTerminated``."""
    lines = source.splitlines()
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try | ast.TryStar):
            continue
        passed = False
        for handler in node.handlers:
            caught = _names(handler.type)
            if caught & _PASS_THROUGH and _re_raises(handler):
                passed = True
                continue
            if "Exception" not in caught or passed:
                continue
            line = lines[handler.lineno - 1]
            if "#" in line and MARKER in line.split("#", 1)[1]:
                continue
            found.append(f"{label}:{handler.lineno}")
    return found


def _modules() -> list[pathlib.Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_walk_finds_the_package() -> None:
    names = {p.relative_to(PACKAGE).as_posix() for p in _modules()}
    assert {"adapters/noop.py", "adapters/tar.py", "adapters/database_load.py"} <= names
    assert "adapters/restore_runner.py" in names


def test_no_broad_handler_swallows_run_terminated() -> None:
    problems: list[str] = []
    for path in _modules():
        problems += violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(PACKAGE))
        )
    assert not problems, (
        f"except Exception without a RunTerminated pass-through: {problems}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    f()\nexcept Exception:\n    pass\n",
        "try:\n    f()\nexcept (ValueError, Exception) as exc:\n    log(exc)\n",
        # a pass-through that does not re-raise is no pass-through
        "try:\n    f()\nexcept RunTerminated:\n    log()\n"
        "except Exception:\n    pass\n",
        # the pass-through must come first
        "try:\n    f()\nexcept Exception:\n    pass\n"
        "except RunTerminated:\n    raise\n",
        # nested inside a function and a with block
        "def g():\n    with x:\n        try:\n            f()\n"
        "        except Exception:\n            return None\n",
    ],
)
def test_a_planted_swallow_is_caught(source: str) -> None:
    assert violations(source, "planted")


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    f()\nexcept RunTerminated:\n    raise\n"
        "except Exception:\n    pass\n",
        "try:\n    f()\nexcept (KeyError, BackupError):\n    raise\n"
        "except Exception:\n    pass\n",
        f"try:\n    f()\nexcept Exception:  # {MARKER}\n    pass\n",
        "try:\n    f()\nexcept BaseException:\n    cleanup()\n    raise\n",
    ],
)
def test_an_accepted_handler_passes(source: str) -> None:
    assert not violations(source, "planted")


def test_the_tracer_guard_lets_a_sigterm_through_and_stays_enabled() -> None:
    guard = GuardedTracer(object())  # type: ignore[arg-type]

    def terminated() -> None:
        msg = "SIGTERM"
        raise RunTerminated(msg)

    with pytest.raises(RunTerminated):
        guard.call(terminated)
    assert guard.call(lambda: "traced") == "traced"


class _TerminatedPolicy:
    source = "custom"
    patterns: tuple[str, ...] = ()

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        msg = "SIGTERM"
        raise RunTerminated(msg)


def test_an_exclusion_decision_interrupted_by_sigterm_stays_a_sigterm(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(RunTerminated):
        _decide(_TerminatedPolicy(), tmp_path, "wp-content/x", is_dir=False)  # type: ignore[arg-type]
