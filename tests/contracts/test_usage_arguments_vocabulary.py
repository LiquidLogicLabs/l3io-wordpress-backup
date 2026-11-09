"""BL-E006-015: ``UsageError.arguments`` has one vocabulary.

On every path a library call reaches, ``arguments`` names the library
parameter (``config_policy``), dotted for a field of one
(``db_tls.ca_file``), or an environment variable (``PATH``). A CLI flag
spelling (``--x``) is allowed only in the modules that only the command line
reaches, :data:`CLI_ONLY`, each with its reason.

Scope: every ``arguments=`` keyword in every call under
``src/l3io/wp/backup/`` (walked, asserted non-empty); each string constant
and f-string head inside its value is judged.

**Not covered:** an argument held in a variable (``arguments=given``) or
built at run time, and whether a ``CLI_ONLY`` module really is unreachable
from the library (reviewed, not derived).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
#: Parameter names, dotted fields, or environment variable names.
ALLOWED = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*|[A-Z][A-Z0-9_]*")
#: Module (relative to the package) -> why only the command line reaches it.
CLI_ONLY = {
    "cli/credentials.py": (
        "the AWS-flag, admin-pair and --new-db-password-file checks; a library "
        "caller passes a CredentialSpec or a Secret, never these flags"
    ),
}


def _literals(value: ast.expr) -> list[str]:
    """Each string constant, and each f-string as its text with ``x`` per field."""
    if isinstance(value, ast.Constant):
        return [value.value] if isinstance(value.value, str) else []
    if isinstance(value, ast.JoinedStr):
        return [
            "".join(
                part.value if isinstance(part, ast.Constant) else "x"
                for part in value.values
            )
        ]
    found: list[str] = []
    for child in ast.iter_child_nodes(value):
        if isinstance(child, ast.expr):
            found += _literals(child)
    return found


def violations(source: str, name: str) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "arguments":
                continue
            for text in _literals(keyword.value):
                if text.startswith("--") and name in CLI_ONLY:
                    continue
                if not ALLOWED.fullmatch(text):
                    bad.append(f"{name}:{node.lineno} {text!r}")
    return bad


MODULES = sorted(PACKAGE.rglob("*.py"))


def test_the_scope_is_found() -> None:
    assert len(MODULES) > 50
    assert all((PACKAGE / name).is_file() for name in CLI_ONLY)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_arguments_name_parameters(path: Path) -> None:
    name = path.relative_to(PACKAGE).as_posix()
    assert violations(path.read_text(encoding="utf-8"), name) == []


def test_a_planted_flag_is_found_outside_the_cli_only_modules() -> None:
    planted = (
        'raise UsageError(m, arguments=("--admin-tls",))\n'
        'raise UsageError(m, arguments=(f"--{p}-x", "db_tls.ca_file", "PATH"))\n'
    )
    assert violations(planted, "adapters/database.py") == [
        "adapters/database.py:1 '--admin-tls'",
        "adapters/database.py:2 '--x-x'",
    ]
    assert violations(planted, "cli/credentials.py") == []
