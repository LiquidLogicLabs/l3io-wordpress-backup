"""BL-E004-060 (AR-6) — a decrypt or extraction is paired with the inner-Manifest check.

``adapters/tar.extract_archive`` and ``decrypt_payload`` never check that an
encrypted payload's inner Manifest is present and equal to the outer one; they
rely on the caller having run :func:`~l3io.wp.backup.adapters.tar.read_inner_manifest`
(ADR-0024). The rule this enforces: every function in the package that calls
either of them also calls ``read_inner_manifest``, or is on :data:`ALLOWED`
with the reason it need not. A stale ``ALLOWED`` entry (a site that no longer
calls either) fails too, so the list cannot outlive its reasons.

**Scope, derived.** Every ``*.py`` under the package root, found by walking
it. A call is recognised by name — a bare name bound by ``from ... import X``
or ``from ... import X as Y``, or any ``<expr>.X(...)`` attribute call — so a
re-export, an alias or a module-qualified call is seen.

**Not covered:** a call through a variable holding the function
(``f = extract_archive; f(...)`` is seen only if bound by an import), and the
callers of the ``SiteFilesCommand.extract`` port, which hold the payload
VERIFY decrypted and checked (``adapters/restore_runner.py``); the port's
adapter is on :data:`ALLOWED` for that reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "l3io" / "wp" / "backup"
GUARDED = frozenset({"extract_archive", "decrypt_payload"})
CHECK = "read_inner_manifest"

#: ``<module path>::<function qualname>`` -> why it needs no inner-Manifest check.
ALLOWED: dict[str, str] = {
    "adapters/site_files.py::LocalSiteFiles.extract": (
        "the SiteFilesCommand port's adapter: delegates unchanged (AD-11); the "
        "restore executor passes it only the payload VERIFY decrypted and "
        "checked with read_inner_manifest"
    ),
    "cli/restore.py::_archive_config": (
        "reads the Archive's wp-config.php before VERIFY so a wrong passphrase "
        "stops the Run before any effect; age authenticates every chunk, the "
        "file is discarded, and VERIFY decrypts again and checks the inner "
        "Manifest (ADR-0024; BL-E004-059 closed wontfix)"
    ),
}


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> imported name, for the guarded functions and the check."""
    wanted = GUARDED | {CHECK}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in wanted:
                    found[alias.asname or alias.name] = alias.name
    return found


def _called(node: ast.Call, aliases: dict[str, str]) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id if func.id in GUARDED | {CHECK} else None)
    if isinstance(func, ast.Attribute) and func.attr in GUARDED | {CHECK}:
        return func.attr
    return None


def _functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every function with its qualname; module-level code as ``<module>``."""
    found: list[tuple[str, ast.AST]] = [("<module>", tree)]

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                found.append((name, child))
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _own_calls(scope: ast.AST) -> list[ast.Call]:
    """Calls in ``scope`` itself, not in a function nested inside it."""
    calls: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.ClassDef) and scope is not node:
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def call_sites(root: Path) -> dict[str, bool]:
    """Each function calling a guarded function -> whether it also checks."""
    sites: dict[str, bool] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        relative = path.relative_to(root).as_posix()
        for name, scope in _functions(tree):
            called = {_called(c, aliases) for c in _own_calls(scope)}
            if called & GUARDED:
                sites[f"{relative}::{name}"] = CHECK in called
    return sites


def unpaired(root: Path, allowed: dict[str, str]) -> list[str]:
    return sorted(
        site
        for site, paired in call_sites(root).items()
        if not paired and site not in allowed
    )


def test_the_walk_finds_the_known_call_sites() -> None:
    """Scope assertion: without it every check below passes over nothing."""
    sites = call_sites(PACKAGE)
    assert sites["cli/verify.py::_authenticate"] is True
    assert sites["adapters/restore_runner.py::_verify"] is True
    assert "adapters/site_files.py::LocalSiteFiles.extract" in sites


def test_every_decrypt_or_extraction_is_paired_or_allowed() -> None:
    assert unpaired(PACKAGE, ALLOWED) == []


def test_no_allowed_site_is_stale() -> None:
    sites = call_sites(PACKAGE)
    stale = [site for site in ALLOWED if site not in sites or sites[site]]
    assert stale == [], "an ALLOWED entry no longer calls without the check"


def _plant(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_a_call_in_a_new_nested_module_is_found(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "api/deep/new.py",
        "from l3io.wp.backup.adapters.tar import extract_archive\n"
        "def go(a, d, v):\n    return extract_archive(a, d, v, roles=frozenset())\n",
    )
    assert unpaired(tmp_path, {}) == ["api/deep/new.py::go"]


def test_an_aliased_import_is_found(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "x.py",
        "from l3io.wp.backup.adapters.tar import decrypt_payload as dp\n"
        "class C:\n    def m(self):\n"
        "        return dp(1, 2, cipher=3, destination=4)\n",
    )
    assert unpaired(tmp_path, {}) == ["x.py::C.m"]


def test_a_module_qualified_call_is_found(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "y.py",
        "from l3io.wp.backup.adapters import tar\n"
        "def go():\n    tar.extract_archive(1, 2, 3, roles=frozenset())\n",
    )
    assert unpaired(tmp_path, {}) == ["y.py::go"]


def test_a_paired_call_passes_and_a_nested_check_does_not_count(
    tmp_path: Path,
) -> None:
    _plant(
        tmp_path,
        "z.py",
        "from l3io.wp.backup.adapters import tar\n"
        "def ok():\n    p = tar.decrypt_payload(1)\n    tar.read_inner_manifest(p)\n"
        "def bad():\n    p = tar.decrypt_payload(1)\n"
        "    def later():\n        tar.read_inner_manifest(p)\n",
    )
    assert unpaired(tmp_path, {}) == ["z.py::bad"]
