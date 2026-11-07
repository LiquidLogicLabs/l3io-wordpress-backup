"""FR-23 — every concrete exported exception carries structured fields.

Owner decision O-1 (b), 2026-11-06: **no exemption list.** Scope is
``api.__all__``'s exception classes that have a construction site of their own
(``tests/exception_scope.py``); a grouping base raised only as its subclasses
(``RefusedError``, ``BackupError``) is not concrete. Each concrete class's
``__init__`` declares at least one named parameter beyond the message
(``message``, ``reason``), and every such parameter is stored as an attribute
of the same name by an ``__init__`` on its MRO, so it is readable after
construction.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import l3io.wp.backup.api as api
from l3io.wp.backup.domain.errors import (
    BackupError,
    DumpFailedError,
    LoadFailedError,
    MalformedManifestError,
    PassphraseRejectedError,
    PayloadDamagedError,
    RefusedError,
    UnknownInterruptedRunError,
    UsageError,
)
from tests.exception_scope import (
    PACKAGE_DIR,
    construction_sites,
    exported_exceptions,
)

#: Free text, not a structured field: the message itself, or why.
MESSAGE_PARAMETERS = frozenset({"message", "reason"})

#: The classes gate O-1 found message-only, and the base raised in their place.
O1_CLASSES = (
    UsageError,
    DumpFailedError,
    LoadFailedError,
    MalformedManifestError,
    PassphraseRejectedError,
    PayloadDamagedError,
    UnknownInterruptedRunError,
)


def _stored(cls: type) -> set[str]:
    """``self.X`` assigned by any ``__init__`` on ``cls``'s MRO."""
    names: set[str] = set()
    for klass in cls.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None or not issubclass(klass, BackupError):
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            names |= {
                t.attr
                for t in targets
                if isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
            }
    return names


def _field_gaps(cls: type) -> str | None:
    """Why ``cls`` lacks structured fields, or ``None`` when it has them."""
    if cls.__init__ in (BaseException.__init__, Exception.__init__):  # type: ignore[comparison-overlap]
        return "message only: no __init__ of its own"
    parameters = [
        p
        for name, p in inspect.signature(cls.__init__).parameters.items()
        if name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    fields = [p.name for p in parameters if p.name not in MESSAGE_PARAMETERS]
    if not fields:
        return f"message only: {[p.name for p in parameters]}"
    unstored = sorted(set(fields) - _stored(cls))
    if unstored:
        return f"not readable as attributes: {unstored}"
    return None


def _concrete() -> dict[str, type]:
    sites = construction_sites(sorted(PACKAGE_DIR.rglob("*.py")), PACKAGE_DIR)
    return {
        name: cls
        for name, cls in exported_exceptions(api.__all__, api).items()
        if name in sites
    }


def test_the_scope_holds_the_o1_classes_and_not_the_grouping_bases() -> None:
    concrete = _concrete()
    assert len(concrete) > len(O1_CLASSES)
    assert set(O1_CLASSES) <= set(concrete.values())
    assert RefusedError not in concrete.values()
    assert BackupError not in concrete.values()


def test_every_concrete_exported_class_carries_structured_fields() -> None:
    gaps = {
        f"{cls.__module__}.{name}": gap
        for name, cls in sorted(_concrete().items())
        if (gap := _field_gaps(cls)) is not None
    }
    assert gaps == {}


@pytest.mark.parametrize("cls", O1_CLASSES, ids=lambda c: c.__name__)
def test_an_o1_class_s_fields_are_readable_after_construction(cls: type) -> None:
    built = {
        UsageError: lambda: UsageError("m", arguments=("--archive",)),
        DumpFailedError: lambda: DumpFailedError("r", database="wp", exit_status=2),
        LoadFailedError: lambda: LoadFailedError("r", database="wp", exit_status=2),
        MalformedManifestError: lambda: MalformedManifestError("r", field="size"),
        PassphraseRejectedError: lambda: PassphraseRejectedError(scheme="age"),
        PayloadDamagedError: lambda: PayloadDamagedError("payload.sha256"),
        UnknownInterruptedRunError: lambda: UnknownInterruptedRunError(
            "r1", recorded_run_id="r2"
        ),
    }[cls]()
    assert _field_gaps(cls) is None
    for name in inspect.signature(cls.__init__).parameters:
        if name not in {"self", *MESSAGE_PARAMETERS}:
            assert hasattr(built, name), name


# --------------------------------------------------------------------------
# Planted cases: each must be reported
# --------------------------------------------------------------------------


class _NoInit(BackupError):
    pass


class _MessageOnly(BackupError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class _ReasonOnly(BackupError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _Unstored(BackupError):
    def __init__(self, path: str) -> None:
        super().__init__(f"bad {path}")


class _StoredByBase(UsageError):
    def __init__(self, flag: str) -> None:
        self.flag = flag
        super().__init__(f"{flag} is wrong", arguments=(flag,))


@pytest.mark.parametrize(
    ("cls", "gap"),
    [
        (_NoInit, "message only: no __init__ of its own"),
        (_MessageOnly, "message only: ['message']"),
        (_ReasonOnly, "message only: ['reason']"),
        (_Unstored, "not readable as attributes: ['path']"),
        (_StoredByBase, None),
    ],
)
def test_a_planted_class_is_judged(cls: type, gap: str | None) -> None:
    assert _field_gaps(cls) == gap


def test_a_class_built_only_in_a_deep_helper_is_in_scope(tmp_path: Path) -> None:
    """Scope attack: concreteness is found wherever the class is built."""
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    (deep / "helper.py").write_text(
        "def failure():\n    return _MessageOnly('m')\n", encoding="utf-8"
    )
    sites = construction_sites(sorted(tmp_path.rglob("*.py")), tmp_path)
    assert "_MessageOnly" in sites
    assert _field_gaps(_MessageOnly) is not None
