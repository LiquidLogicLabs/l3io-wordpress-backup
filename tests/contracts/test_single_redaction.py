"""ADR-0018 — the package owns exactly one redaction implementation.

``adapters/redaction.py`` is the only module that may contain the marker
literal ``"***"`` or define a function whose name contains ``redact``. This is
what keeps ``domain/`` and ``core/`` free of redaction code, and Epic 6's
tracing adapter calling the registry rather than writing its own.

Scope: every ``*.py`` under the package, walked rather than listed. The marker
check sees string constants (f-string parts included); it cannot see a marker
assembled at runtime, which review covers.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "adapters" / "redaction.py"
MARKER = "***"


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and MARKER in node.value
        ):
            found.append(f"line {node.lineno}: the {MARKER!r} marker")
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and "redact" in node.name.lower()
        ):
            found.append(f"line {node.lineno}: defines {node.name}()")
    return found


def _violations(root: Path, owner: Path) -> list[str]:
    return sorted(
        f"{path.relative_to(root)} {offence}"
        for path in root.rglob("*.py")
        if path != owner
        for offence in _offences(path)
    )


def test_the_scope_is_the_whole_package() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert OWNER in modules, f"{OWNER} is missing: the one implementation moved?"
    layers = {p.relative_to(PACKAGE).parts[0] for p in modules if p.parent != PACKAGE}
    assert {"domain", "core", "ports", "adapters", "cli", "api"} <= layers


def test_the_owner_really_holds_the_implementation() -> None:
    """If the owner had none, the exclusion below would hide nothing real."""
    assert _offences(OWNER)


def test_no_other_module_redacts() -> None:
    offenders = _violations(PACKAGE, OWNER)
    assert not offenders, (
        f"redaction belongs to adapters/redaction.py alone (ADR-0018): {offenders}"
    )


def test_the_guard_fires_in_a_nested_layer(tmp_path: Path) -> None:
    """A planted violation deep in the tree, and one in a pure layer."""
    root = tmp_path / "pkg"
    (root / "adapters").mkdir(parents=True)
    (root / "domain" / "deep").mkdir(parents=True)
    owner = root / "adapters" / "redaction.py"
    owner.write_text("def redact(t):\n    return '***'\n", encoding="utf-8")
    (root / "domain" / "deep" / "x.py").write_text(
        "def _scrub_redacted(t):\n    return t\n", encoding="utf-8"
    )
    (root / "domain" / "y.py").write_text(
        "def mask(u):\n    return f'{u}:***'\n", encoding="utf-8"
    )
    found = _violations(root, owner)
    assert len(found) == 2, found
    assert any("deep" in f for f in found)
