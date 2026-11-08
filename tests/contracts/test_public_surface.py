"""FR-24 — what is public, and what is internal by layer.

A name is public if and only if it is in ``l3io.wp.backup.__all__`` or
``l3io.wp.backup.api.__all__``. Every other module is internal **by the layer
it lives in**, whatever its name (spine, Consistency Conventions).

Every scope here is derived, never listed:

* the **public set** is the two ``__all__`` lists, read at run time;
* the **layers** are the ``layers`` contract in ``pyproject.toml``'s
  ``[tool.importlinter]``, read with ``tomllib``. Every layer but ``api`` is
  internal, so a layer added to the contract is classified without an edit
  here, and a layer with no directory (or a directory with no layer) fails;
* the **modules** are every ``*.py`` under ``src/l3io/wp/backup/``
  (``tests/exception_scope.py``'s walk, the one the AD-14 contracts use).

What this file does not check: that a name's *documentation* is right. That
is ``test_api_docs.py`` (headings, parameters, the CLI-layer note), and the
prose itself is reviewed, not tested.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tomllib
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

import l3io.wp.backup as root
import l3io.wp.backup.api as api
from tests.contracts.test_repr_clean import (
    _module_classes,
    defines_its_own_repr,
    holds_a_secret,
)
from tests.exception_scope import PACKAGE, PACKAGE_DIR, PROJECT_ROOT, SRC, module_names

PUBLIC_LAYER = "api"
#: Names of the two modules whose ``__all__`` *is* the public surface.
PUBLIC_MODULES = (PACKAGE, f"{PACKAGE}.{PUBLIC_LAYER}")
#: Fields whose value is a credential (story: "named password, passphrase,
#: identity or identities"), matched as a word inside the field name so that
#: ``new_db_password`` is in scope too.
SECRET_WORDS = ("password", "passphrase", "identity", "identities")


# --------------------------------------------------------------------------
# Derived scopes
# --------------------------------------------------------------------------


def layers_from(pyproject: dict[str, object]) -> list[str]:
    """The layer short names in the one ``layers`` contract over the package.

    An entry may hold siblings (``"a : b"``, or ``"a | b"`` for independent
    ones); each is split and stripped, so an entry read whole cannot leave a
    layer unclassified.
    """
    tool = pyproject["tool"]
    assert isinstance(tool, dict)
    contracts = [
        c
        for c in tool["importlinter"]["contracts"]
        if c["type"] == "layers"
        and all(e.strip().startswith(PACKAGE + ".") for e in c["layers"])
    ]
    assert len(contracts) == 1, (
        f"expected exactly one import-linter `layers` contract over {PACKAGE}, "
        f"found {[c['name'] for c in contracts]}"
    )
    found: list[str] = []
    for entry in contracts[0]["layers"]:
        for part in entry.replace("|", ":").split(":"):
            name = part.strip()
            assert name.startswith(PACKAGE + "."), f"unexpected layer {name!r}"
            found.append(name.removeprefix(PACKAGE + "."))
    return found


def layers() -> list[str]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return layers_from(tomllib.load(handle))


def internal_layers() -> set[str]:
    return set(layers()) - {PUBLIC_LAYER}


def classify(module: str, internal: Iterable[str]) -> str | None:
    """``root``, ``public``, ``api-implementation``, ``internal`` or ``None``.

    ``root`` is the package and the modules directly in it (``_version``):
    only ``l3io.wp.backup.__all__`` is public there.
    """
    if not (module == PACKAGE or module.startswith(PACKAGE + ".")):
        return None
    rest = module.removeprefix(PACKAGE).removeprefix(".").split(".")
    if rest == [""] or (len(rest) == 1 and (PACKAGE_DIR / f"{rest[0]}.py").is_file()):
        return "root"
    if rest == [PUBLIC_LAYER]:
        return "public"
    if rest[0] == PUBLIC_LAYER:
        return "api-implementation"
    if rest[0] in set(internal):
        return "internal"
    return None


def public_names() -> dict[str, object]:
    """Every public name, mapped to its object."""
    found: dict[str, object] = {}
    for module in (root, api):
        for name in module.__all__:
            found[name] = getattr(module, name)
    return found


def own_docstring(obj: object) -> str:
    """The docstring written in the source, not one a decorator synthesised.

    ``@dataclass`` fills an empty class ``__doc__`` with its signature, so a
    class's docstring is read from its ``class`` statement instead.
    """
    if isinstance(obj, type):
        node = _module_classes(obj.__module__).get(obj.__qualname__)
        if node is None:
            message = f"no class statement for {obj.__qualname__}"
            raise AssertionError(message)
        return ast.get_docstring(node) or ""
    return inspect.getdoc(obj) or ""


def cli_ban_contract(pyproject: dict[str, Any]) -> dict[str, Any]:
    """The import-linter ``forbidden`` contract that keeps ``cli/`` off ``api/``.

    ``import-linter`` resolves the imports (absolute, relative, nested, under
    ``TYPE_CHECKING``, and indirect chains), so this test only proves the
    contract is present and shaped right; ``lint-imports`` enforces it.
    """
    contracts = [
        c
        for c in pyproject["tool"]["importlinter"]["contracts"]
        if c["type"] == "forbidden"
        and f"{PACKAGE}.cli" in c["source_modules"]
        and f"{PACKAGE}.{PUBLIC_LAYER}" in c["forbidden_modules"]
    ]
    assert len(contracts) == 1, "no import-linter contract forbids cli/ -> api/"
    contract = contracts[0]
    assert not contract.get("allow_indirect_imports", False)
    return contract


def secret_fields_shown(cls: type) -> list[str]:
    """Credential-named annotated fields that the class's ``repr`` would show."""
    names = [
        n
        for klass in cls.__mro__
        for n in getattr(klass, "__annotations__", {})
        if any(word in n.lower() for word in SECRET_WORDS)
    ]
    if not names or defines_its_own_repr(cls):
        return []
    hidden = (
        {f.name for f in dataclasses.fields(cls) if not f.repr}
        if dataclasses.is_dataclass(cls)
        else set()
    )
    return sorted(set(names) - hidden)


# --------------------------------------------------------------------------
# The layers, and the classification of every module
# --------------------------------------------------------------------------


def test_the_layers_contract_parses_to_every_known_layer() -> None:
    """A floor, so that a parse returning nothing cannot pass vacuously."""
    parsed = layers()
    assert {"domain", "core", "ports", "adapters", "cli", PUBLIC_LAYER} <= set(parsed)
    assert len(parsed) == len(set(parsed)), f"a layer is listed twice: {parsed}"


def test_a_sibling_entry_is_split_rather_than_read_whole() -> None:
    planted = {
        "tool": {
            "importlinter": {
                "contracts": [
                    {
                        "name": "planted",
                        "type": "layers",
                        "layers": [
                            f"{PACKAGE}.cli : {PACKAGE}.api",
                            f"{PACKAGE}.a | {PACKAGE}.b",
                        ],
                    }
                ]
            }
        }
    }
    assert layers_from(planted) == ["cli", "api", "a", "b"]


def test_every_layer_has_a_directory_and_every_directory_a_layer() -> None:
    directories = {
        path.parent.relative_to(PACKAGE_DIR).parts[0]
        for path in PACKAGE_DIR.rglob("*.py")
        if path.parent != PACKAGE_DIR
    }
    declared = set(layers())
    assert declared - directories == set(), (
        f"layers in pyproject.toml with no directory: {sorted(declared - directories)}"
    )
    assert directories - declared == set(), (
        f"directories under {PACKAGE_DIR.relative_to(SRC)} that no layer in the "
        f"import-linter contract classifies: {sorted(directories - declared)}"
    )


def test_every_module_is_classified() -> None:
    internal = internal_layers()
    modules = module_names(SRC, PACKAGE_DIR)
    kinds = {module: classify(module, internal) for module in modules}

    assert [m for m, kind in kinds.items() if kind is None] == []
    # The scope half: each kind is reached, so the walk covered the tree.
    assert set(kinds.values()) == {"root", "public", "api-implementation", "internal"}
    assert {m.split(".")[3] for m, k in kinds.items() if k == "internal"} == internal


def test_a_module_outside_every_layer_is_unclassified() -> None:
    internal = internal_layers()
    assert classify(f"{PACKAGE}.newlayer.thing", internal) is None
    assert classify(f"{PACKAGE}.newlayer", internal) is None
    assert classify(f"{PACKAGE}._version", internal) == "root"
    assert classify(f"{PACKAGE}.api.errors", internal) == "api-implementation"
    assert classify(f"{PACKAGE}.cli.restore", internal) == "internal"


# --------------------------------------------------------------------------
# __all__ integrity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_every_public_name_resolves_once(module_name: str) -> None:
    module = importlib.import_module(module_name)
    names = list(module.__all__)
    assert names, f"{module_name}.__all__ is empty"
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"{module_name}.__all__ lists {duplicates} twice"
    missing = [n for n in names if not hasattr(module, n)]
    assert missing == [], f"{module_name}.__all__ names {missing}, which do not resolve"


def test_the_two_lists_do_not_overlap() -> None:
    assert set(root.__all__) & set(api.__all__) == set()


def test_every_package_declares_all_and_a_docstring() -> None:
    packages = [
        m
        for m in module_names(SRC, PACKAGE_DIR)
        if (SRC / m.replace(".", "/") / "__init__.py").is_file()
    ]
    assert PACKAGE in packages
    assert f"{PACKAGE}.{PUBLIC_LAYER}" in packages
    for name in packages:
        module = importlib.import_module(name)
        assert "__all__" in vars(module), f"{name} declares no __all__"
        assert (module.__doc__ or "").strip(), f"{name}/__init__.py has no docstring"


def test_every_public_class_and_callable_has_a_docstring() -> None:
    undocumented = [
        name
        for name, obj in public_names().items()
        if (isinstance(obj, type) or callable(obj)) and not own_docstring(obj).strip()
    ]
    assert undocumented == []


@dataclasses.dataclass
class _Undocumented:
    x: int


def test_a_dataclass_without_a_docstring_is_caught() -> None:
    """``@dataclass`` synthesises a ``__doc__``; the check must not accept it."""
    assert _Undocumented.__doc__, "dataclass no longer synthesises a __doc__"
    assert own_docstring(_Undocumented) == ""
    assert own_docstring(api.SiteDescriptor).strip()


def test_every_public_object_is_a_declaration_not_an_instance() -> None:
    """No adapter instance, registry or resolved credential is exported."""
    instances = {
        name: type(obj).__name__
        for name, obj in public_names().items()
        if not isinstance(obj, type | types.FunctionType | types.UnionType | str)
    }
    assert instances == {}


# --------------------------------------------------------------------------
# cli/ never imports api/
# --------------------------------------------------------------------------


def _pyproject() -> dict[str, Any]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_an_import_linter_contract_forbids_cli_importing_api() -> None:
    cli_ban_contract(_pyproject())


def test_the_contract_catches_a_planted_import(tmp_path: Path) -> None:
    """Plant ``cli/nested/planted.py`` importing ``api`` in a copy of the tree.

    The import sits in a *nested* subpackage and is *relative*, so the
    contract's reach (every module below ``cli``) is what is tested, not
    only its rule. The planted module exists only in the copy, so its name in
    the report proves the copy was the tree analysed.
    """
    site = tmp_path / "site"
    shutil.copytree(
        PACKAGE_DIR,
        site / PACKAGE.replace(".", "/"),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    nested = site / PACKAGE.replace(".", "/") / "cli" / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text('"""Planted."""\n', "utf-8")
    (nested / "planted.py").write_text("from ...api import errors\n", "utf-8")

    contract = cli_ban_contract(_pyproject())
    config = tmp_path / "importlinter.toml"
    config.write_text(
        "[tool.importlinter]\n"
        f'root_packages = ["{PACKAGE}"]\n'
        "[[tool.importlinter.contracts]]\n"
        f"name = {json.dumps(contract['name'])}\n"
        'type = "forbidden"\n'
        f"source_modules = {json.dumps(contract['source_modules'])}\n"
        f"forbidden_modules = {json.dumps(contract['forbidden_modules'])}\n",
        "utf-8",
    )
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; from importlinter.cli import lint_imports_command; "
            "sys.exit(lint_imports_command())",
            "--config",
            str(config),
            "--no-cache",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{PACKAGE}.cli.nested.planted" in result.stdout, result.stdout


# --------------------------------------------------------------------------
# Security: nothing public shows a credential in its repr
# --------------------------------------------------------------------------


def _public_classes() -> list[type]:
    return [o for o in public_names().values() if isinstance(o, type)]


def test_no_public_class_shows_a_credential_in_its_repr() -> None:
    shown = {c.__name__: secret_fields_shown(c) for c in _public_classes()}
    assert {k: v for k, v in shown.items() if v} == {}


def test_every_public_secret_holder_defines_its_own_repr() -> None:
    """E002-S01-006's walked check, scoped to the public set."""
    holders = [c for c in _public_classes() if holds_a_secret(c)]
    assert "ExplicitCredentialSpec" in {c.__name__ for c in holders}
    assert [c.__name__ for c in holders if not defines_its_own_repr(c)] == []


def test_the_repr_check_catches_a_shown_credential() -> None:
    @dataclasses.dataclass
    class Shown:
        new_db_password: str

    @dataclasses.dataclass
    class Hidden:
        new_db_password: str = dataclasses.field(repr=False)

    class Plain:
        identity: str

    assert secret_fields_shown(Shown) == ["new_db_password"]
    assert secret_fields_shown(Hidden) == []
    assert secret_fields_shown(Plain) == ["identity"]
    # The public set reaches a class this applies to, so the walk has scope.
    assert any(
        any(w in n for w in SECRET_WORDS for n in getattr(c, "__annotations__", {}))
        for c in _public_classes()
    )


def test_run_context_is_not_public() -> None:
    """BL-E008-028 (owner decision 2026-11-07): no entry point takes or returns
    one; it returns with ``open_run()`` post-v1 (BL-E006-025)."""
    from l3io.wp.backup import api

    assert "RunContext" not in api.__all__
    assert not hasattr(api, "RunContext")
