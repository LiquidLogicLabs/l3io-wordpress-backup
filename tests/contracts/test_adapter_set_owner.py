"""ADR-0016 — one container of bound adapters: ``ports/adapter_set.AdapterSet``.

An AST walk of the package finds exactly one ``class AdapterSet``, in
``ports/adapter_set.py``, and no other class whose annotated fields include two
or more **port halves** — a field annotated with a Protocol defined under
``ports/`` whose name ends in ``Query``, ``Command`` or ``Probe`` (AD-4's
split). The set is derived from ``ports/`` itself, so a port added later is
covered without editing this test. Observation ports (``Tracer``) and the
``Clock`` are not halves: the Run Context holds those by design.

Scope: every ``*.py`` under ``src/l3io/wp/backup/``, walked and asserted
non-empty.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
OWNER = PACKAGE / "ports" / "adapter_set.py"


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(b, ast.Name) and b.id == "Protocol")
        or (isinstance(b, ast.Attribute) and b.attr == "Protocol")
        for b in node.bases
    )


HALF_SUFFIXES = ("Query", "Command", "Probe")


def port_names(ports: Path) -> set[str]:
    """Every query/command Protocol class defined under ``ports/``."""
    return {
        node.name
        for path in ports.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        and _is_protocol(node)
        and node.name.endswith(HALF_SUFFIXES)
    }


def _annotation_names(annotation: ast.expr) -> set[str]:
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(annotation)
        if isinstance(n, ast.Name | ast.Attribute)
    } | {
        n.value
        for n in ast.walk(annotation)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _containers(root: Path, ports: set[str]) -> list[str]:
    """``path:Class`` for each class named AdapterSet or holding 2+ port fields."""
    found: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or _is_protocol(node):
                continue
            fields = [
                item
                for item in node.body
                if isinstance(item, ast.AnnAssign)
                and _annotation_names(item.annotation) & ports
            ]
            if node.name == "AdapterSet" or len(fields) >= 2:
                found.append(f"{path.relative_to(root)}:{node.name}")
    return sorted(found)


def test_the_scope_is_the_whole_package() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert OWNER in modules


def test_the_port_set_is_derived_and_holds_the_sprint_one_ports() -> None:
    ports = port_names(PACKAGE / "ports")
    assert {
        "ArchiveStoreQuery",
        "ArchiveStoreCommand",
        "ArchiveProbe",
        "DatabaseQuery",
        "DatabaseCommand",
    } <= ports
    assert "Tracer" not in ports


def test_exactly_one_container_and_it_is_ports_adapter_set() -> None:
    found = _containers(PACKAGE, port_names(PACKAGE / "ports"))
    assert found == ["ports/adapter_set.py:AdapterSet"]


def test_the_guard_sees_a_second_container_anywhere(tmp_path: Path) -> None:
    (tmp_path / "ports").mkdir()
    (tmp_path / "cli" / "deep").mkdir(parents=True)
    (tmp_path / "ports" / "p.py").write_text(
        "class StoreQuery(Protocol):\n    ...\n"
        "class StoreCommand(typing.Protocol):\n    ...\n"
        "class Clock(Protocol):\n    ...\n",
        encoding="utf-8",
    )
    (tmp_path / "ports" / "adapter_set.py").write_text(
        "class AdapterSet:\n    store: StoreQuery\n", encoding="utf-8"
    )
    (tmp_path / "cli" / "deep" / "x.py").write_text(
        "@dataclass\nclass Bundle:\n    a: StoreQuery | None\n    b: 'StoreCommand'\n"
        "class AdapterSet:\n    pass\n"
        "class Context:\n    a: StoreQuery\n    c: Clock\n",
        encoding="utf-8",
    )
    found = _containers(tmp_path, port_names(tmp_path / "ports"))
    assert found == [
        "cli/deep/x.py:AdapterSet",
        "cli/deep/x.py:Bundle",
        "ports/adapter_set.py:AdapterSet",
    ]
