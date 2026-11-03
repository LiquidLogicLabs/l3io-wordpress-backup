"""AD-4/AG-12 — every port is a query or a command, and every command has a no-op.

The convention (``ports/__init__.py``): each capability is a ``*Query`` and a
``*Command`` ``typing.Protocol``; a Protocol that is neither is named, with its
reason, on the **single-role allowance** in that module's docstring. Then the
Dry Run's half: every ``*Command`` Protocol has exactly one implementation in
``adapters/noop.py``, implementing every member with the Protocol's
parameters, and every ``AdapterSet`` field typed as a ``*Command`` is filled
with a no-op by ``dry_run_adapters`` while every other field is the real set's
own instance.

**Scope, derived.** The Protocols are found by walking every ``*.py`` under
``ports/`` (asserted non-empty) and importing it; the fields come from
``dataclasses.fields(AdapterSet)``, and a field is a command when any type in
its resolved annotation (a union member, an ``Optional``, a generic argument)
is one of the discovered ``*Command`` Protocols (``tests/port_roles.py``); the
allowance is
parsed from the docstring and checked to name only Protocols that exist. Each
check is also run over a planted tree or class — a misnamed Protocol in a
nested directory with no ``__init__.py``, a command with no no-op, an
``AdapterSet``-shaped class whose constructor forgets a command — so the
guards are shown to see what they claim to.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
import typing
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.ports.adapter_set import AdapterSet
from tests.port_roles import PORTS, SRC, command_fields, protocols

SUFFIXES = ("Query", "Command")


def allowance(docstring: str) -> set[str]:
    """The names on the single-role allowance in ``ports/__init__.py``."""
    _, marker, rest = docstring.partition("Single-role allowance")
    assert marker, "ports/__init__.py states no single-role allowance"
    section = rest.split("\n\n", 2)[1]
    return set(re.findall(r"^- ``(\w+)``:", section, re.MULTILINE))


def misnamed(found: dict[str, type], allowed: set[str]) -> list[str]:
    return sorted(n for n in found if not n.endswith(SUFFIXES) and n not in allowed)


def members(protocol: type) -> dict[str, object]:
    """The Protocol's own public methods and properties."""
    return {
        name: value
        for name, value in vars(protocol).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or isinstance(value, property))
    }


def _parameters(value: object) -> list[str]:
    function = value.fget if isinstance(value, property) else value
    assert callable(function)
    return list(inspect.signature(function).parameters)


def implementations(protocol: type, module: ModuleType) -> list[str]:
    """Classes in ``module`` defining every member of ``protocol`` alike."""
    wanted = members(protocol)
    found = []
    for name, cls in vars(module).items():
        if not inspect.isclass(cls) or cls.__module__ != module.__name__:
            continue
        if all(
            hasattr(cls, member)
            and isinstance(getattr(cls, member), property) == isinstance(spec, property)
            and _parameters(inspect.getattr_static(cls, member)) == _parameters(spec)
            for member, spec in wanted.items()
        ):
            found.append(name)
    return found


def missing_noops(found: dict[str, type], module: ModuleType) -> dict[str, list[str]]:
    """Each ``*Command`` without exactly one implementation in ``module``."""
    return {
        name: impls
        for name, protocol in found.items()
        if name.endswith("Command")
        and len(impls := implementations(protocol, module)) != 1
    }


def unfilled(
    container: type,
    construct: Callable[[typing.Any], object],
    command_types: Collection[type] | None = None,
) -> list[str]:
    """Fields ``construct`` gets wrong: a command not no-op, or a query copied."""
    real = container(**{f.name: object() for f in dataclasses.fields(container)})
    made = construct(real)
    commands = set(command_fields(container, command_types))
    wrong = []
    for f in dataclasses.fields(container):
        value = getattr(made, f.name)
        if f.name in commands:
            if type(value).__module__ != noop.__name__:
                wrong.append(f.name)
        elif value is not getattr(real, f.name):
            wrong.append(f.name)
    return sorted(wrong)


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------


def test_the_scope_is_every_port_module() -> None:
    assert list(PORTS.rglob("*.py")), f"no port module under {PORTS}"
    found = protocols(SRC, PORTS)
    assert {"DatabaseQuery", "DatabaseCommand", "ArchiveWriterCommand"} <= set(found)


def test_every_protocol_is_a_query_a_command_or_on_the_allowance() -> None:
    import l3io.wp.backup.ports as ports

    assert ports.__doc__ is not None
    allowed = allowance(ports.__doc__)
    found = protocols(SRC, PORTS)
    assert not misnamed(found, allowed), (
        f"AD-4 — {misnamed(found, allowed)} is neither *Query nor *Command; "
        "split it, or justify it on the single-role allowance"
    )
    assert allowed <= set(found), (
        f"the allowance names {sorted(allowed - set(found))}, which are not Protocols"
    )


def test_every_command_has_exactly_one_noop() -> None:
    found = protocols(SRC, PORTS)
    assert [n for n in found if n.endswith("Command")], "no *Command Protocol found"
    assert missing_noops(found, noop) == {}


def test_dry_run_adapters_fills_every_command_field_with_a_noop() -> None:
    commands = command_fields(AdapterSet)
    assert {"archive_store_command", "database_command", "archive_writer"} <= set(
        commands
    )
    assert unfilled(AdapterSet, dry_run_adapters) == []


# --------------------------------------------------------------------------
# Scope attacks
# --------------------------------------------------------------------------


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    package = tmp_path / "planted_ports"
    (package / "deep").mkdir(parents=True)
    (package / "deep" / "widget.py").write_text(
        "from typing import Protocol\n\n"
        "class Widget(Protocol):\n    def spin(self) -> None: ...\n\n"
        "class WidgetCommand(Protocol):\n    def spin(self, n: int) -> None: ...\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield package
    for name in [m for m in sys.modules if m.startswith("planted_ports")]:
        del sys.modules[name]


def test_a_misnamed_protocol_in_a_nested_directory_is_seen(planted: Path) -> None:
    found = protocols(planted.parent, planted)
    assert set(found) == {"Widget", "WidgetCommand"}
    assert misnamed(found, set()) == ["Widget"]
    assert misnamed(found, {"Widget"}) == []


def test_a_command_without_a_noop_is_seen(planted: Path) -> None:
    found = protocols(planted.parent, planted)
    assert missing_noops(found, noop) == {"WidgetCommand": []}


def test_a_noop_with_the_wrong_parameters_does_not_count(tmp_path: Path) -> None:
    class SpinCommand(Protocol):
        def spin(self, n: int) -> None: ...

    module = ModuleType("planted_noop")
    exec(  # noqa: S102 - a fixed planted module
        "class A:\n    def spin(self) -> None: ...\n"
        "class B:\n    def spin(self, n: int) -> None: ...\n",
        module.__dict__,
    )
    assert implementations(SpinCommand, module) == ["B"]


def test_the_allowance_parser_reads_only_its_section() -> None:
    doc = (
        "Intro naming ``Nope``.\n\nSingle-role allowance — x:\n\n"
        "- ``Clock``: a query.\n- ``Tracer``: observation.\n\n- ``Later``: no.\n"
    )
    assert allowance(doc) == {"Clock", "Tracer"}


class WidgetCommand(Protocol):
    def spin(self, n: int) -> None: ...


class NamedLikeACommand:
    """Named ``...Command`` but not a discovered Protocol: not a command."""


@pytest.mark.parametrize(
    "annotation",
    [
        WidgetCommand,
        WidgetCommand | None,
        typing.Optional[WidgetCommand],  # noqa: UP045 - the spelling under test
        typing.Union[None, WidgetCommand],  # noqa: UP007 - the spelling under test
        list[WidgetCommand],
        typing.Annotated[WidgetCommand | None, "doc"],
    ],
    ids=["bare", "pipe-none", "optional", "union", "generic", "annotated"],
)
def test_a_constructor_that_forgets_a_command_is_seen(annotation: object) -> None:
    @dataclasses.dataclass(frozen=True)
    class Planted:
        store_query: typing.Any
        widget_command: typing.Any
        named_command: typing.Any
        tracer: typing.Any

    Planted.__annotations__["widget_command"] = annotation
    Planted.__annotations__["named_command"] = NamedLikeACommand
    commands = {WidgetCommand}
    assert command_fields(Planted, commands) == ["widget_command"]
    assert unfilled(Planted, lambda real: real, commands) == ["widget_command"]
    assert unfilled(
        Planted, lambda real: dataclasses.replace(real, tracer=object()), commands
    ) == ["tracer", "widget_command"]


def test_the_command_scope_is_the_discovered_protocols() -> None:
    """A field is a command because its type is a discovered Protocol."""
    found = protocols(SRC, PORTS)

    @dataclasses.dataclass(frozen=True)
    class Planted:
        writer: typing.Any
        query: typing.Any

    Planted.__annotations__["writer"] = found["ArchiveWriterCommand"] | None
    Planted.__annotations__["query"] = found["DatabaseQuery"]
    assert command_fields(Planted) == ["writer"]
