"""FR-14 — one credential chain and no backup-owned provider: database's.

Every chain is ``l3io.wp.database.CredentialChain`` and every source one of
database's classes (E005-S01-001). This guard fails when the package grows

- a **second chain**: an ``except`` handler naming ``CredentialsAbsentError``
  inside a ``for``/``while`` loop, the defining shape of "try the next source";
- a **second provider**: a class with a method ``resolve`` annotated to return
  ``ResolvedCredentials``.

Scope: every ``*.py`` under ``src/l3io/wp/backup/``, walked, asserted
non-empty. Three exemptions, by qualified name: ``adapters/database.py``'s
``_RegisteringSource``, the registering wrapper around database's sources
(ADR-0018), which provides nothing of its own;
``adapters/credential_providers.py``'s ``_Resolved``, which replays the
answer a WordPress chain gave a backup started as root before it switched
users (owner decision 67), under that chain's label, and so provides nothing
of its own either; and ``adapters/database.py``'s ``WpConfigCredentials``
(owner decision 47), ``wp-config.php``'s ``DB_USER``/``DB_PASSWORD`` read
through ``l3io-wordpress-config``'s ``get_value``, so a ``getenv_docker()``
pair resolves against the Run's ``environ``. Database 0.3.4's ``WpConfigFile``
reads them with ``get()``, which returns such a definition's source text as
the value; the exemption goes when database's own source resolves lookups.
The exemptions are themselves checked: an entry that no longer exists or no
longer matches fails, so a renamed class cannot silently widen the guard.

**What this cannot see**, and review covers: a chain built with
``contextlib.suppress(CredentialsAbsentError)`` or by recursion rather than a
loop; a handler catching a base class (``DatabaseError``) of the absence
error; a method bound at runtime; and a ``resolve`` whose return annotation is
missing or spelled through an alias.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
PACKAGE = SRC / "l3io" / "wp" / "backup"
ABSENT: Final = "CredentialsAbsentError"
RESOLVED: Final = "ResolvedCredentials"
EXEMPT: Final = frozenset(
    {
        "l3io.wp.backup.adapters.credential_providers._Resolved",
        "l3io.wp.backup.adapters.database._RegisteringSource",
        "l3io.wp.backup.adapters.database.WpConfigCredentials",
    }
)

_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _names(node: ast.AST | None) -> set[str]:
    """Every bare or attribute name, and string annotation, under ``node``."""
    if node is None:
        return set()
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.update(re.findall(r"\w+", sub.value))
    return found


def _chains(tree: ast.Module, module: str) -> list[str]:
    found: list[str] = []

    def walk(node: ast.AST, in_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                in_loop
                and isinstance(child, ast.ExceptHandler)
                and ABSENT in _names(child.type)
            ):
                found.append(f"{module}:{child.lineno}")
            walk(child, in_loop or isinstance(child, _LOOPS))

    walk(tree, False)
    return found


def _providers(tree: ast.Module, module: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name == "resolve"
                and RESOLVED in _names(item.returns)
            ):
                found.append(f"{module}.{node.name}")
    return found


def scan(package: Path, src: Path) -> tuple[list[str], list[str], int]:
    """``(chains, providers, files scanned)`` for every module under ``package``."""
    chains: list[str] = []
    providers: list[str] = []
    files = sorted(package.rglob("*.py"))
    for path in files:
        module = ".".join(path.relative_to(src).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        chains.extend(_chains(tree, module))
        providers.extend(_providers(tree, module))
    return chains, providers, len(files)


def stale(exempt: frozenset[str], providers: list[str]) -> list[str]:
    """Exemptions that name no class the scan still flags."""
    return sorted(exempt - set(providers))


def test_the_package_writes_no_chain_and_no_provider() -> None:
    chains, providers, files = scan(PACKAGE, SRC)
    assert files > 50, f"scanned only {files} files under {PACKAGE}"
    assert chains == [], f"a second credential chain: {chains}"
    assert sorted(set(providers) - EXEMPT) == [], "a backup-owned provider"


def test_every_exemption_still_exists_and_still_matches() -> None:
    _, providers, _ = scan(PACKAGE, SRC)
    assert stale(EXEMPT, providers) == []


_PLANTED = """
from l3io.wp.database import CredentialsAbsentError, ResolvedCredentials
from l3io.wp import database as wpdb


def first(sources):
    for source in sources:
        try:
            return source.resolve()
        except wpdb.CredentialsAbsentError:
            continue


class Vault:
    source = "vault"

    def resolve(self) -> ResolvedCredentials:
        raise NotImplementedError


class Quoted:
    def resolve(self) -> "wpdb.ResolvedCredentials":
        raise NotImplementedError
"""


def test_a_planted_chain_and_provider_deep_in_the_tree_are_caught(
    tmp_path: Path,
) -> None:
    # Attacks the scope: the violation sits in a new, nested subpackage.
    src = tmp_path / "src"
    nested = src / "l3io" / "wp" / "backup" / "adapters" / "vault"
    nested.mkdir(parents=True)
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "provider.py").write_text(_PLANTED, encoding="utf-8")
    chains, providers, files = scan(src / "l3io" / "wp" / "backup", src)
    assert files == 2
    assert chains == ["l3io.wp.backup.adapters.vault.provider:10"]
    assert providers == [
        "l3io.wp.backup.adapters.vault.provider.Vault",
        "l3io.wp.backup.adapters.vault.provider.Quoted",
    ]


def test_an_absence_handler_outside_a_loop_is_not_a_chain(tmp_path: Path) -> None:
    src = tmp_path / "src"
    package = src / "pkg"
    package.mkdir(parents=True)
    (package / "one.py").write_text(
        "def f(s):\n    try:\n        return s.resolve()\n"
        "    except CredentialsAbsentError:\n        return None\n",
        encoding="utf-8",
    )
    assert scan(package, src)[0] == []


def test_a_renamed_exemption_is_stale() -> None:
    _, providers, _ = scan(PACKAGE, SRC)
    renamed = frozenset({"l3io.wp.backup.adapters.database._RenamedSource"})
    assert stale(renamed, providers) == sorted(renamed)
