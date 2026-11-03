"""AD-7 item 3 — every type that holds a secret defines its own ``__repr__``.

A generated ``repr`` prints every field, so a type that holds a credential
source, ``ResolvedCredentials``, a password, a passphrase or an identity
defines ``__repr__`` itself (without the secret). The classes are **walked**:
every class defined in every module of the package, matched on its class and
``__init__`` annotations and on its field and slot names. A secret held under
a name outside :data:`SECRET_FIELDS` and an unannotated attribute are not seen;
``test_credential_hygiene.py`` checks the produced ``repr`` text.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import functools
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

import pytest

import l3io.wp.backup as package

SECRET_TYPES = ("ResolvedCredentials", "CredentialSource")
SECRET_FIELDS = frozenset(
    {"password", "passphrase", "identity", "identities", "secrets"}
)
#: A class named for what it holds — ``ResolvedPassphrase.value``.
SECRET_NAMES = ("Passphrase", "Password")
#: Matched by name but holding no secret. Each entry must still exist and
#: still match, so a stale exemption fails rather than lingering.
NOT_SECRETS = {
    "l3io.wp.backup.adapters.tar.ArchiveMemberSource": (
        "identity is the file's (st_dev, st_ino), not an age identity"
    ),
}
#: Present today; the walk must reach each, or its scope has shrunk.
KNOWN_HOLDERS = frozenset(
    {"ExplicitCredentialSpec", "ResolvedPassphrase", "WpDatabase", "SecretRegistry"}
)


def _nested(cls: type) -> list[type]:
    inner = [
        value
        for value in vars(cls).values()
        if isinstance(value, type)
        and value.__qualname__.startswith(f"{cls.__qualname__}.")
    ]
    return [cls, *(c for i in inner for c in _nested(i))]


def _classes(paths: list[str] | None = None, prefix: str | None = None) -> list[type]:
    """Every class defined in the package's modules, nested classes included."""
    found: list[type] = []
    for info in pkgutil.walk_packages(
        package.__path__ if paths is None else paths,
        f"{package.__name__}." if prefix is None else prefix,
    ):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == module.__name__:
                found.extend(_nested(value))
    return found


def _names(cls: type) -> set[str]:
    names = set(inspect.get_annotations(cls))
    slots = cls.__dict__.get("__slots__", ())
    names.update((slots,) if isinstance(slots, str) else slots)
    if dataclasses.is_dataclass(cls):
        names.update(f.name for f in dataclasses.fields(cls))
    return {name.lstrip("_") for name in names}


def _annotation_text(cls: type) -> str:
    texts = [str(v) for v in inspect.get_annotations(cls).values()]
    init = cls.__dict__.get("__init__")
    if init is not None:
        texts.extend(str(v) for v in inspect.get_annotations(init).values())
    return " ".join(texts)


def holds_a_secret(cls: type) -> bool:
    if issubclass(cls, BaseException | enum.Enum):
        return False  # exception text: test_credential_hygiene.py; enums: labels
    text = _annotation_text(cls)
    return (
        bool(_names(cls) & SECRET_FIELDS)
        or any(t in text for t in SECRET_TYPES)
        or any(word in cls.__name__ for word in SECRET_NAMES)
    )


def _class_defs(tree: ast.AST, prefix: str = "") -> dict[str, ast.ClassDef]:
    """Every ``class`` in a module, keyed by its ``__qualname__``."""
    found: dict[str, ast.ClassDef] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            found[prefix + node.name] = node
            found.update(_class_defs(node, f"{prefix}{node.name}."))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.update(_class_defs(node, f"{prefix}{node.name}.<locals>."))
    return found


@functools.cache
def _module_classes(module: str) -> dict[str, ast.ClassDef]:
    source = Path(inspect.getfile(sys.modules[module])).read_text(encoding="utf-8")
    return _class_defs(ast.parse(source))


def defines_its_own_repr(cls: type) -> bool:
    """A ``def __repr__`` in the class's own body, read from its source.

    ``@dataclass`` puts a generated ``__repr__`` into the class ``__dict__``,
    so the class object cannot tell the two apart; the source can.
    """
    node = _module_classes(cls.__module__).get(cls.__qualname__)
    assert node is not None, f"no source for {cls.__qualname__}"
    return any(
        isinstance(item, ast.FunctionDef) and item.name == "__repr__"
        for item in node.body
    )


def _offenders(classes: list[type]) -> list[str]:
    return sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in classes
        if holds_a_secret(cls)
        and not defines_its_own_repr(cls)
        and f"{cls.__module__}.{cls.__qualname__}" not in NOT_SECRETS
    )


def test_the_walk_reaches_every_known_holder() -> None:
    holders = {cls.__name__ for cls in _classes() if holds_a_secret(cls)}
    missing = KNOWN_HOLDERS - holders
    assert not missing, f"the walk no longer sees {sorted(missing)}"


def test_every_exemption_is_still_matched() -> None:
    matched = {
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in _classes()
        if holds_a_secret(cls)
    }
    stale = set(NOT_SECRETS) - matched
    assert not stale, f"remove stale exemptions: {sorted(stale)}"


def test_every_secret_holder_defines_its_own_repr() -> None:
    offenders = _offenders(_classes())
    assert not offenders, f"define __repr__ without the secret: {offenders}"


def test_the_guard_fires_on_each_kind_of_holder() -> None:
    @dataclasses.dataclass
    class ByField:
        passphrase: str

    class BySlot:
        __slots__ = ("_identity",)

    class ByInitAnnotation:
        def __init__(self, source: CredentialSource) -> None:  # noqa: F821
            self.source = source

    @dataclasses.dataclass
    class Clean:
        username: str

        def __repr__(self) -> str:
            return "Clean()"

    @dataclasses.dataclass
    class ResolvedPlantedPassword:
        value: str

    planted: list[type] = [ByField, BySlot, ByInitAnnotation, ResolvedPlantedPassword]
    assert len(_offenders([*planted, Clean])) == len(planted)


def test_the_walk_reaches_a_nested_holder_in_a_planted_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "planted_reprs"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "m.py").write_text(
        "import dataclasses\n"
        "class Outer:\n"
        "    @dataclasses.dataclass\n"
        "    class Inner:\n"
        "        password: str\n"
        "    def __repr__(self):\n"
        "        return 'Outer()'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    classes = _classes([str(root)], "planted_reprs.")
    assert _offenders(classes) == ["planted_reprs.m.Outer.Inner"]
