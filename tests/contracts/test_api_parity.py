"""FR-22 / AD-22 — every bound CLI operation is a library call, typed in its own terms.

**The scope is read from the source of truth, never listed here.** The
operations are the subcommands ``cli/parser.py::build_parser()`` registers,
less the bare-subcommand keys of ``cli/parser.py::NOT_YET_IMPLEMENTED`` (a
registered but unbound subcommand, ``doctor`` today). Each must be a callable
in ``l3io.wp.backup.api.__all__``. When a binding story removes a key from
that mapping, this test requires the API function.

The entry points' annotations are read with ``typing.get_type_hints`` and
unions and generics unpacked, so the set of package classes a caller must be
able to import comes from the signatures:

- every ``l3io.wp.backup`` class named there is in ``api.__all__`` (A15);
- none is an ``l3io.wp.database`` type (ADR-0015 amendment);
- no parameter is ``args``/``argv`` or an ``argparse.Namespace`` (FR-22).

Re-exported classes are the defining layer's, by identity: none is defined
in ``api/``. Each rule has a planted case that attacks its scope.
"""

from __future__ import annotations

import argparse
import inspect
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.cli.parser import NOT_YET_IMPLEMENTED, build_parser

#: AD-22's frozen subcommands: an empty walk must not pass.
FROZEN = frozenset({"backup", "restore"})
PACKAGE = "l3io.wp.backup."
DATABASE = "l3io.wp.database"


def _subcommands(parser: argparse.ArgumentParser) -> frozenset[str]:
    return frozenset(
        name
        for action in parser._actions  # noqa: SLF001 - argparse has no public walk
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        for name in action.choices
    )


def _unbound(not_yet: Mapping[str, str]) -> frozenset[str]:
    """The bare-subcommand keys (``"doctor"``), not ``"restore --flag"``."""
    return frozenset(key for key in not_yet if " " not in key)


def _scope(parser: argparse.ArgumentParser, not_yet: Mapping[str, str]) -> set[str]:
    registered = _subcommands(parser)
    stale = _unbound(not_yet) - registered
    assert not stale, f"NOT_YET_IMPLEMENTED names no subparser: {sorted(stale)}"
    return set(registered - _unbound(not_yet))


def _missing(scope: Iterable[str], module: types.ModuleType) -> list[str]:
    exported = set(getattr(module, "__all__", ()))
    return sorted(
        name
        for name in scope
        if name not in exported or not callable(getattr(module, name, None))
    )


def _entry_points(module: types.ModuleType) -> dict[str, Callable[..., object]]:
    return {
        name: obj
        for name in module.__all__
        if inspect.isfunction(obj := getattr(module, name))
    }


def _classes(hint: object) -> Iterable[type]:
    """Every class a hint names, through unions, aliases and generics."""
    if isinstance(hint, type) and typing.get_origin(hint) is None:
        yield hint
        return
    origin = typing.get_origin(hint)
    if isinstance(origin, type):
        yield origin
    for arg in typing.get_args(hint):
        if isinstance(arg, list):  # Callable's parameter list
            for item in arg:
                yield from _classes(item)
        else:
            yield from _classes(arg)


def _named(function: Callable[..., object]) -> set[type]:
    hints = typing.get_type_hints(function)
    return {cls for hint in hints.values() for cls in _classes(hint)}


def _unexported(module: types.ModuleType) -> list[str]:
    exported = set(module.__all__)
    return sorted(
        f"{name}: {cls.__module__}.{cls.__qualname__}"
        for name, function in _entry_points(module).items()
        for cls in _named(function)
        if cls.__module__.startswith(PACKAGE)
        and (cls.__name__ not in exported or getattr(module, cls.__name__) is not cls)
    )


def _database_types(module: types.ModuleType) -> list[str]:
    return sorted(
        f"{name}: {cls.__module__}.{cls.__qualname__}"
        for name, function in _entry_points(module).items()
        for cls in _named(function)
        if cls.__module__ == DATABASE or cls.__module__.startswith(DATABASE + ".")
    )


def _argv_parameters(module: types.ModuleType) -> list[str]:
    found: list[str] = []
    for name, function in _entry_points(module).items():
        hints = typing.get_type_hints(function)
        for parameter in inspect.signature(function).parameters:
            named = set(_classes(hints.get(parameter, object)))
            if parameter in {"args", "argv"} or argparse.Namespace in named:
                found.append(f"{name}({parameter})")
    return found


# --------------------------------------------------------------------------
# The rules, over the real package
# --------------------------------------------------------------------------


def test_the_walk_finds_the_frozen_subcommands() -> None:
    scope = _scope(build_parser(), NOT_YET_IMPLEMENTED)
    assert scope >= FROZEN


def test_every_bound_subcommand_is_an_api_function() -> None:
    assert _missing(_scope(build_parser(), NOT_YET_IMPLEMENTED), api) == []


def test_every_package_class_in_a_signature_is_exported() -> None:
    assert _entry_points(api).keys() >= FROZEN
    assert _unexported(api) == []


def test_no_signature_names_a_database_type() -> None:
    assert _database_types(api) == []


def test_no_entry_point_takes_a_command_line() -> None:
    assert _argv_parameters(api) == []


def test_every_exported_class_is_the_defining_layers() -> None:
    """``UnexpectedError`` alone is api's own: its one raise site is api's
    ``public_boundary`` (E006-S01-002), so api is the layer that defines it."""
    redefined = sorted(
        name
        for name in api.__all__
        if isinstance(obj := getattr(api, name), type)
        and obj.__module__.startswith("l3io.wp.backup.api")
        and obj is not api.UnexpectedError
    )
    assert redefined == []


# --------------------------------------------------------------------------
# Planted cases: each attacks the rule's scope
# --------------------------------------------------------------------------


def _with_subcommand(name: str) -> argparse.ArgumentParser:
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            action.add_parser(name)
    return parser


def test_a_new_subcommand_without_an_api_function_fails() -> None:
    parser = _with_subcommand("prune")
    assert _missing(_scope(parser, NOT_YET_IMPLEMENTED), api) == ["prune"]


def test_a_new_subcommand_marked_unbound_passes() -> None:
    parser = _with_subcommand("prune")
    not_yet = dict(NOT_YET_IMPLEMENTED) | {"prune": "not yet"}
    assert _missing(_scope(parser, not_yet), api) == []


def test_a_stale_unbound_key_fails() -> None:
    not_yet = dict(NOT_YET_IMPLEMENTED) | {"vanished": "not yet"}
    with pytest.raises(AssertionError, match="vanished"):
        _scope(build_parser(), not_yet)


def test_doctor_is_bound_and_has_its_api_function() -> None:
    """E008-S01-001 bound ``doctor``; its library twin is ``api.doctor``."""
    assert "doctor" not in NOT_YET_IMPLEMENTED
    assert "doctor" in _scope(build_parser(), NOT_YET_IMPLEMENTED)
    assert _missing(_scope(build_parser(), NOT_YET_IMPLEMENTED), api) == []


class _Hidden:
    """A package class an entry point names but ``__all__`` omits."""


_Hidden.__module__ = "l3io.wp.backup.domain.hidden"


class _DatabaseType:
    pass


_DatabaseType.__module__ = "l3io.wp.database.credentials"


def _planted_module(**functions: Callable[..., object]) -> types.ModuleType:
    module = types.ModuleType("planted")
    module.__dict__.update(functions)
    module.__all__ = list(functions)  # type: ignore[attr-defined]
    return module


def _hidden_in_a_union(*, x: int | dict[str, list[_Hidden]] | None = None) -> None:
    del x


def _database_in_a_callable(*, f: Callable[[_DatabaseType], None]) -> None:
    del f


def _takes_args(args: argparse.Namespace | None, *, argv: list[str]) -> None:
    del args, argv


def _ns(*, options: Any | argparse.Namespace) -> None:  # noqa: ANN401
    del options


def test_the_annotation_rules_fire_on_planted_signatures() -> None:
    module = _planted_module(
        hidden=_hidden_in_a_union,
        database=_database_in_a_callable,
        takes=_takes_args,
        ns=_ns,
    )
    assert _unexported(module) == ["hidden: l3io.wp.backup.domain.hidden._Hidden"]
    assert _database_types(module) == [
        "database: l3io.wp.database.credentials._DatabaseType"
    ]
    assert _argv_parameters(module) == ["takes(args)", "takes(argv)", "ns(options)"]
