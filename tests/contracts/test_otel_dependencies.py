"""AD-24/AD-8 — the base install is the OpenTelemetry API; nothing configures tracing.

``pyproject.toml`` is read with ``tomllib`` and every requirement is parsed
with ``packaging.requirements.Requirement`` — never prefix-matched, because
the allowed ``opentelemetry-exporter-otlp-json-file`` shares the forbidden
umbrella's prefix:

- ``opentelemetry-api`` is in ``dependencies``;
- no ``dependencies`` entry is ``opentelemetry-sdk`` or an
  ``opentelemetry-exporter-*`` distribution;
- no requirement in ``dependencies`` or any ``optional-dependencies`` extra is
  the umbrella ``opentelemetry-exporter-otlp`` or ``grpcio``.

An AST scan of every module under ``src/l3io/wp/backup/`` (walked, never
listed) finds no import of ``opentelemetry.sdk`` or
``opentelemetry.exporter`` at any scope. Each rule is shown to fire on a
planted violation. ``make verify-install`` is the installed-tree half: the
built wheel's base install finds no ``opentelemetry.sdk``.
"""

from __future__ import annotations

import ast
import shutil
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"

API = "opentelemetry-api"
FORBIDDEN_ANYWHERE = frozenset({"opentelemetry-exporter-otlp", "grpcio"})
FORBIDDEN_MODULES = ("opentelemetry.sdk", "opentelemetry.exporter")


def _names(requirements: list[str]) -> list[str]:
    return [canonicalize_name(Requirement(r).name) for r in requirements]


def _dependency_violations(pyproject: Path) -> list[str]:
    project: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
        "project"
    ]
    base = _names(project.get("dependencies", []))
    found: list[str] = []
    if API not in base:
        found.append(f"{API} is not a base dependency")
    found += [
        f"base dependency {name}"
        for name in base
        if name == "opentelemetry-sdk" or name.startswith("opentelemetry-exporter-")
    ]
    groups = {"dependencies": base} | {
        f"extra {extra}": _names(reqs)
        for extra, reqs in project.get("optional-dependencies", {}).items()
    }
    found += [
        f"{where}: {name}"
        for where, names in groups.items()
        for name in names
        if name in FORBIDDEN_ANYWHERE
    ]
    return found


def _imports(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            yield from (
                (node.lineno, f"{node.module}.{alias.name}") for alias in node.names
            )


def _import_violations(root: Path) -> list[str]:
    return sorted(
        f"{path.relative_to(root)}:{line}: {name}"
        for path in root.rglob("*.py")
        for line, name in _imports(ast.parse(path.read_text(encoding="utf-8")))
        if name == "opentelemetry.sdk"
        or name == "opentelemetry.exporter"
        or name.startswith(tuple(f"{m}." for m in FORBIDDEN_MODULES))
    )


def test_the_scope_is_the_package_on_disk() -> None:
    modules = set(PACKAGE.rglob("*.py"))
    assert PACKAGE / "adapters" / "tracing.py" in modules
    assert len(modules) > 50


def test_the_dependencies_hold() -> None:
    assert _dependency_violations(PYPROJECT) == []


def test_the_otel_extra_is_installable_and_allowed() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    names = _names(project["optional-dependencies"]["otel"])
    assert "opentelemetry-sdk" in names
    assert "opentelemetry-exporter-otlp-json-file" in names


def test_nothing_in_the_package_imports_the_sdk_or_an_exporter() -> None:
    assert _import_violations(PACKAGE) == []


def test_a_planted_dependency_is_reported(tmp_path: Path) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    planted = tmp_path / "pyproject.toml"
    planted.write_text(
        text.replace(
            '"opentelemetry-api>=1.40,<2",',
            '"opentelemetry-sdk>=1.40,<2",\n  "opentelemetry_exporter_OTLP>=1",',
            1,
        ).replace(
            "otel = [\n",
            "otel = [\n  \"grpcio>=1; python_version >= '3.11'\",\n",
            1,
        ),
        encoding="utf-8",
    )
    assert planted.read_text(encoding="utf-8") != text
    assert _dependency_violations(planted) == [
        f"{API} is not a base dependency",
        "base dependency opentelemetry-sdk",
        "base dependency opentelemetry-exporter-otlp",
        "dependencies: opentelemetry-exporter-otlp",
        "extra otel: grpcio",
    ]


def test_a_planted_import_deep_in_the_tree_is_reported(tmp_path: Path) -> None:
    copy = tmp_path / "backup"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert _import_violations(copy) == []
    deep = copy / "adapters" / "nested"
    deep.mkdir()
    (deep / "host.py").write_text(
        "def configure():\n"
        "    from opentelemetry.sdk.trace import TracerProvider\n"
        "    import opentelemetry.exporter.otlp.json_file as f\n"
        "    from opentelemetry import sdk\n",
        encoding="utf-8",
    )
    assert _import_violations(copy) == [
        "adapters/nested/host.py:2: opentelemetry.sdk.trace",
        "adapters/nested/host.py:2: opentelemetry.sdk.trace.TracerProvider",
        "adapters/nested/host.py:3: opentelemetry.exporter.otlp.json_file",
        "adapters/nested/host.py:4: opentelemetry.sdk",
    ]
