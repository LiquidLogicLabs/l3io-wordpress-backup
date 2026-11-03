"""ADR-0015 amendment (2026-10-27) — credential sources are built in one place.

Only ``adapters/database.py`` (and, once Epic 5 adds it,
``adapters/credential_providers.py``) calls database's ``Explicit``,
``Environment``, ``AwsSecretsManager``, ``WpConfigFile`` or
``CredentialChain``. ``cli/`` and ``api/`` pass backup-owned
``CredentialSpec`` values, and no public request type is annotated with a
database type.

Scope: every module under ``src/l3io/wp/backup/``, walked. A call is matched
by the called name, and an ``import ... as`` of a constructor from database is
an offence by itself, so a static alias is seen. A name bound at runtime
(``f = getattr(wpdb, "Explicit")``) is not; review covers that.

**One exemption, by resolved origin (E002-S02-009).** ``l3io.wp.config``
also has a ``WpConfigFile`` — the reader/writer, not a credential source.
A call is exempt only when its name resolves, within the module, to
``l3io.wp.config``: a bare name imported from it, or an attribute of a name
bound to it. Every other spelling of ``WpConfigFile`` is still an offence.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pkgutil
import typing
from pathlib import Path

import pytest

import l3io.wp.backup as package

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
CONSTRUCTORS = frozenset(
    {"Explicit", "Environment", "AwsSecretsManager", "WpConfigFile", "CredentialChain"}
)
BUILDER = PACKAGE / "adapters" / "database.py"
PROVIDERS = PACKAGE / "adapters" / "credential_providers.py"
DATABASE_MODULE = "l3io.wp.database"
CONFIG_MODULE = "l3io.wp.config"


def _excluded() -> set[Path]:
    """The builders, each present on disk when excluded."""
    excluded = {BUILDER}
    if PROVIDERS.exists():
        excluded.add(PROVIDERS)
    return excluded


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _config_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to ``l3io.wp.config`` itself, and to its ``WpConfigFile``."""
    modules: set[str] = set()
    classes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname
                for alias in node.names
                if alias.name == CONFIG_MODULE and alias.asname
            )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full == CONFIG_MODULE:
                    modules.add(alias.asname or alias.name)
                elif node.module == CONFIG_MODULE and alias.name == "WpConfigFile":
                    classes.add(alias.asname or alias.name)
    return modules, classes


def _is_config_class(call: ast.Call, modules: set[str], classes: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in classes
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in modules
    return False


def _offence(
    node: ast.AST, config: tuple[set[str], set[str]] = (set(), set())
) -> str | None:
    if isinstance(node, ast.Call) and _called_name(node) in CONSTRUCTORS:
        if _is_config_class(node, *config):
            return None
        return f"calls {_called_name(node)}"
    if (
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(DATABASE_MODULE)
        and any(alias.name in CONSTRUCTORS for alias in node.names)
    ):
        return "imports a credential constructor"
    return None


def _offenders(root: Path, excluded: set[Path]) -> list[str]:
    found = []
    for path in root.rglob("*.py"):
        if path in excluded:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        config = _config_bindings(tree)
        found.extend(
            f"{path.relative_to(root)}:{node.lineno} {offence}"
            for node in ast.walk(tree)
            if (offence := _offence(node, config)) is not None
        )
    return sorted(found)


def test_the_scope_is_the_whole_package_and_the_builder_exists() -> None:
    modules = list(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    assert BUILDER.is_file()
    assert all(path.is_file() for path in _excluded())


def test_the_builder_really_builds_sources() -> None:
    """Otherwise excluding it would hide nothing, and the rule nothing."""
    assert _offenders(PACKAGE, set()), "adapters/database.py builds no source?"


def test_only_the_builders_construct_credential_sources() -> None:
    offenders = _offenders(PACKAGE, _excluded())
    assert not offenders, (
        "build credential sources in adapters/database.py and pass a "
        f"CredentialSpec (ADR-0015): {offenders}"
    )


def test_the_guard_fires_on_a_planted_cli_module(tmp_path: Path) -> None:
    (tmp_path / "cli").mkdir()
    (tmp_path / "adapters").mkdir()
    builder = tmp_path / "adapters" / "database.py"
    builder.write_text("wpdb.Explicit(u, p)\n", encoding="utf-8")
    (tmp_path / "cli" / "x.py").write_text(
        "from l3io.wp import database as wpdb\n"
        "a = wpdb.CredentialChain(wpdb.Explicit(u, p))\n",
        encoding="utf-8",
    )
    (tmp_path / "cli" / "y.py").write_text(
        "from l3io.wp.database import WpConfigFile as Reader\nReader(p)\n",
        encoding="utf-8",
    )
    found = _offenders(tmp_path, {builder})
    assert found == [
        "cli/x.py:2 calls CredentialChain",
        "cli/x.py:2 calls Explicit",
        "cli/y.py:1 imports a credential constructor",
    ]


def test_config_wp_config_file_is_exempt_only_by_resolved_origin(
    tmp_path: Path,
) -> None:
    """Scope attack: the exemption must not cover database's class by name."""
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "ok.py").write_text(
        "from l3io.wp.config import WpConfigFile\n"
        "from l3io.wp import config as cfg\n"
        "WpConfigFile(p)\ncfg.WpConfigFile(p)\n",
        encoding="utf-8",
    )
    (tmp_path / "adapters" / "bad.py").write_text(
        "from l3io.wp.config import WpConfigFile\n"
        "from l3io.wp import database as wpdb\n"
        "wpdb.WpConfigFile(p)\n",
        encoding="utf-8",
    )
    (tmp_path / "adapters" / "unbound.py").write_text(
        "WpConfigFile(p)\n", encoding="utf-8"
    )
    found = _offenders(tmp_path, set())
    assert found == [
        "adapters/bad.py:3 calls WpConfigFile",
        "adapters/unbound.py:1 calls WpConfigFile",
    ]


# --------------------------------------------------------------------------
# No database type on a public request
# --------------------------------------------------------------------------


def _request_types(paths: list[str], prefix: str) -> list[type]:
    """Every class named ``*Request`` defined in the package — any kind of class."""
    found: list[type] = []
    for info in pkgutil.walk_packages(paths, prefix):
        module = importlib.import_module(info.name)
        found.extend(
            value
            for name, value in vars(module).items()
            if name.endswith("Request")
            and isinstance(value, type)
            and value.__module__ == module.__name__
        )
    return found


def _declared_requests(root: Path) -> set[str]:
    """``class *Request`` anywhere in the source, from the AST: the truth."""
    return {
        node.name
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and node.name.endswith("Request")
    }


def _request_offenders(root: Path, paths: list[str], prefix: str) -> list[str]:
    requests = _request_types(paths, prefix)
    unseen = _declared_requests(root) - {cls.__name__ for cls in requests}
    assert not unseen, f"declared but not reached by the walk: {sorted(unseen)}"
    return [o for cls in requests for o in _offending_hints(cls)]


def _database_types(hint: object) -> list[str]:
    found = [
        repr(hint)
        for candidate in (hint, typing.get_origin(hint))
        if getattr(candidate, "__module__", "").startswith(DATABASE_MODULE)
    ]
    for arg in typing.get_args(hint):
        found.extend(_database_types(arg))
    return found


def _offending_hints(cls: type, localns: dict[str, object] | None = None) -> list[str]:
    hints = typing.get_type_hints(cls, localns=localns)
    return [
        f"{cls.__name__}.{name}: {found}"
        for name, hint in hints.items()
        for found in _database_types(hint)
    ]


def test_no_public_request_type_carries_a_database_type() -> None:
    if not _declared_requests(PACKAGE):
        pytest.skip("no class *Request in the source yet (E002-S01-004 adds one)")
    offenders = _request_offenders(
        PACKAGE, list(package.__path__), f"{package.__name__}."
    )
    assert not offenders, f"carry a CredentialSpec, not a database type: {offenders}"


def test_the_request_walk_reaches_a_planted_plain_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip(DATABASE_MODULE)
    root = tmp_path / "planted_requests"
    (root / "deep").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "deep" / "__init__.py").write_text("", encoding="utf-8")
    (root / "deep" / "m.py").write_text(
        "from l3io.wp.database import CredentialSource\n"
        "class XRequest:\n    credentials: CredentialSource | None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    found = _request_offenders(root, [str(root)], "planted_requests.")
    assert len(found) == 1


def test_the_hint_check_sees_a_nested_database_type() -> None:
    wpdb = pytest.importorskip(DATABASE_MODULE)

    @dataclasses.dataclass
    class PlantedRequest:
        credentials: tuple[CredentialSource, ...] | None  # noqa: F821

    found = _offending_hints(
        PlantedRequest, {"CredentialSource": wpdb.CredentialSource}
    )
    assert found
