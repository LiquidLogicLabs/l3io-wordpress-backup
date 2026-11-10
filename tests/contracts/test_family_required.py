"""Owner decision 76 — the family is a required dependency; ``aws`` stays optional.

From 1.0.0 a plain ``pip install l3io-wordpress-backup`` backs up and restores:
``l3io-wordpress-database[encryption]``, ``l3io-wordpress-config`` and the
``l3io-age-passphrase`` binder are in ``[project.dependencies]``. The
``database`` extra is kept, empty, so ``[database]`` installs keep working. The
``aws`` extra (decision 71) still brings boto3, and the base install must not.

Everything here is read from ``pyproject.toml`` and ``uv.lock``; no package
list is kept by hand. The family is every requirement named ``l3io-*``:

- every family package any extra names is also a base dependency, so an extra
  can only add a family package's own extra (``aws`` = ``database[aws]``);
- the ``database`` extra is declared and empty;
- the base install's resolved closure in ``uv.lock`` holds every family package
  and no ``boto3``/``botocore``, and the closure with ``aws`` holds boto3;
- the session guard in ``tests/conftest.py`` finds every base requirement of
  the installed project met, so no ``importorskip`` of the family can skip.

Each rule is proven by planted violations, including ones on its scope. The
installed-wheel half (a clean environment from PyPI imports the database layer
and pulls no boto3) is ci.yml's verify-install job and ``make verify-wheel``.
"""

from __future__ import annotations

import copy
import importlib.util
import pathlib
import tomllib
from collections.abc import Callable, Iterable
from types import ModuleType
from typing import Any

import pytest
from packaging.requirements import Requirement

from tests.conftest import unmet_base_requirements

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: What the ``aws`` extra exists to bring, and the base install must not.
AWS_ONLY = frozenset({"boto3", "botocore"})


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _family(reqs: Iterable[str]) -> set[str]:
    return {r.name for r in map(Requirement, reqs) if r.name.startswith("l3io-")}


Roots = Iterable[tuple[str, Iterable[str]]]


def _closure(lock: dict[str, Any], roots: Roots) -> set[str]:
    """Every package ``roots`` pull in ``uv.lock``, each root's extras too.

    Markers are ignored, so a platform-only dependency counts: the check is the
    conservative superset.
    """
    packages = {p["name"]: p for p in lock["package"]}
    seen: set[str] = set()
    todo = [(name, tuple(extras)) for name, extras in roots]
    done: set[tuple[str, tuple[str, ...]]] = set()
    while todo:
        name, extras = todo.pop()
        if (name, extras) in done:
            continue
        done.add((name, extras))
        seen.add(name)
        pkg = packages[name]
        edges = list(pkg.get("dependencies", []))
        for extra in extras:
            edges.extend(pkg.get("optional-dependencies", {}).get(extra, []))
        todo.extend((e["name"], tuple(e.get("extra", ()))) for e in edges)
    return seen


def _own_closure(lock: dict[str, Any], own: str, extras: Iterable[str]) -> set[str]:
    return _closure(lock, [(own, extras)]) - {own}


def _findings(pyproject: dict[str, Any], lock: dict[str, Any]) -> list[str]:
    project = pyproject["project"]
    own = project["name"]
    base = list(project.get("dependencies", []))
    optional: dict[str, list[str]] = project.get("optional-dependencies", {})
    found: list[str] = []
    family_base = _family(base)
    if not family_base:
        found.append("dependencies: no family package is a base dependency")
    for extra, reqs in sorted(optional.items()):
        for name in sorted(_family(reqs) - family_base):
            found.append(f"extra {extra}: family package {name} is not in the base")
    if "database" not in optional:
        found.append("extra database: not declared; [database] installs would warn")
    elif optional["database"]:
        found.append(f"extra database: not empty: {optional['database']}")
    in_base = _own_closure(lock, own, ())
    for name in sorted(family_base - in_base):
        found.append(f"uv.lock: base install does not pull {name}")
    for name in sorted(AWS_ONLY & in_base):
        found.append(f"uv.lock: base install pulls {name}")
    if "aws" not in optional:
        found.append("extra aws: not declared")
    elif "boto3" not in _own_closure(lock, own, ("aws",)):
        found.append("uv.lock: the aws extra does not pull boto3")
    return found


def test_the_family_is_required_and_aws_is_not() -> None:
    pyproject, lock = _pyproject(), _lock()
    own = pyproject["project"]["name"]
    family = _family(pyproject["project"]["dependencies"])
    assert len(_own_closure(lock, own, ())) > len(family), "uv.lock looks empty"
    assert _findings(pyproject, lock) == []


def _base(pyproject: dict[str, Any]) -> list[str]:
    deps: list[str] = pyproject["project"]["dependencies"]
    return deps


def _optional(pyproject: dict[str, Any]) -> dict[str, list[str]]:
    extras: dict[str, list[str]] = pyproject["project"]["optional-dependencies"]
    return extras


def _family_back_into_an_extra(pyproject: dict, _lock: dict) -> None:
    base = _base(pyproject)
    moved = [r for r in base if Requirement(r).name == "l3io-wordpress-config"]
    base[:] = [r for r in base if r not in moved]
    _optional(pyproject)["database"] = moved


def _new_family_extra(pyproject: dict, _lock: dict) -> None:
    _optional(pyproject)["planted"] = ["l3io-planted>=1"]


def _drop_alias(pyproject: dict, _lock: dict) -> None:
    del _optional(pyproject)["database"]


def _alias_not_empty(pyproject: dict, _lock: dict) -> None:
    _optional(pyproject)["database"] = ["l3io-wordpress-database[aws]>=0.3.5,<0.4"]


def _lock_edge(name: str, edge: dict[str, Any]) -> Callable[[dict, dict], None]:
    def plant(pyproject: dict, lock: dict) -> None:
        for pkg in lock["package"]:
            if pkg["name"] == name:
                pkg.setdefault("dependencies", []).append(edge)

    return plant


def _unlock_edge(name: str, dropped: str) -> Callable[[dict, dict], None]:
    def plant(pyproject: dict, lock: dict) -> None:
        for pkg in lock["package"]:
            if pkg["name"] == name:
                pkg["dependencies"] = [
                    d for d in pkg["dependencies"] if d["name"] != dropped
                ]

    return plant


def _aws_brings_nothing(pyproject: dict, lock: dict) -> None:
    for pkg in lock["package"]:
        if pkg["name"] == pyproject["project"]["name"]:
            pkg["optional-dependencies"]["aws"] = []


PLANTS = {
    "a family package moved back into the database extra": _family_back_into_an_extra,
    "a new extra naming a family package": _new_family_extra,
    "the database alias removed": _drop_alias,
    "the database alias given a requirement": _alias_not_empty,
    # Attacks the scope: boto3 arrives TRANSITIVELY, through database's aws
    # extra on a base edge, not as a direct requirement.
    "the base takes database[aws]": _lock_edge(
        "l3io-wordpress-backup", {"name": "l3io-wordpress-database", "extra": ["aws"]}
    ),
    # config is reached only by its direct edge (the binder also arrives
    # through database[encryption], so dropping its edge alone changes nothing).
    "the base lock loses config": _unlock_edge(
        "l3io-wordpress-backup", "l3io-wordpress-config"
    ),
    "the aws extra brings nothing": _aws_brings_nothing,
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_planted_violation_is_reported(plant: str) -> None:
    pyproject, lock = copy.deepcopy(_pyproject()), copy.deepcopy(_lock())
    PLANTS[plant](pyproject, lock)
    assert _findings(pyproject, lock) != []


def test_every_base_requirement_of_the_installed_project_is_met() -> None:
    """The session guard's own check, run here so its result is visible."""
    assert unmet_base_requirements() == []


@pytest.mark.parametrize(
    ("planted", "reported"),
    [
        ("l3io-planted-absent>=1", True),
        ("structlog>=999", True),
        # Attacks the scope: a marker true here is a base requirement too.
        ('l3io-planted-absent>=1; python_version >= "3"', True),
        # An extra's requirement is never the base install's.
        ('l3io-planted-absent>=1; extra == "aws"', False),
    ],
)
def test_the_session_guard_reports_a_planted_unmet_requirement(
    planted: str, reported: bool
) -> None:
    unmet = unmet_base_requirements(extra_lines=[planted])
    assert unmet == ([str(Requirement(planted))] if reported else [])


# --------------------------------------------------------------------------
# tools/check_base_install.py — run against the built wheel in a clean
# environment (ci.yml verify-install, publish.yml smoke, make verify-wheel);
# here against fake metadata, so its rules are proven without a network.
# --------------------------------------------------------------------------

TOOL = ROOT / "tools" / "check_base_install.py"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_base_install", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: A metadata world shaped like the real one; each test edits a copy.
WORLD: dict[str, Any] = {
    "requires": {
        "l3io-wordpress-backup": [
            "structlog>=26",
            "l3io-wordpress-database[encryption]>=0.3.5,<0.4",
            "l3io-wordpress-config>=0.1.7,<0.2",
            'l3io-wordpress-database[aws]>=0.3.5,<0.4; extra == "aws"',
            'opentelemetry-sdk>=1.40; extra == "otel"',
        ],
        "l3io-wordpress-database": [
            "pymysql>=1.2",
            'boto3>=1.34; extra == "aws"',
            'pyrage>=1.4; extra == "encryption"',
            'l3io-age-passphrase>=0.1.1; extra == "encryption"',
        ],
    },
    "versions": {
        "l3io-wordpress-backup": "1.0.0",
        "structlog": "26.1.0",
        "l3io-wordpress-database": "0.3.5",
        "l3io-wordpress-config": "0.1.7",
        "pymysql": "1.2.0",
        "pyrage": "1.4.0",
        "l3io-age-passphrase": "0.1.1",
    },
    "extras": ["aws", "database", "otel"],
}


def _run(world: dict[str, Any]) -> list[str]:
    found: list[str] = _tool().findings(
        world["requires"].get,
        world["versions"].get,
        lambda _name: world["extras"],
    )
    return found


def test_the_base_install_tool_passes_a_clean_base() -> None:
    assert _run(WORLD) == []


def _without(name: str) -> Callable[[dict[str, Any]], None]:
    def plant(world: dict[str, Any]) -> None:
        del world["versions"][name]

    return plant


def _with(name: str, version: str) -> Callable[[dict[str, Any]], None]:
    def plant(world: dict[str, Any]) -> None:
        world["versions"][name] = version

    return plant


def _no_extras(world: dict[str, Any]) -> None:
    world["extras"] = []


TOOL_PLANTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "the database library missing": _without("l3io-wordpress-database"),
    "config missing": _without("l3io-wordpress-config"),
    # Attacks the scope: reached only through database's encryption extra.
    "the binder missing": _without("l3io-age-passphrase"),
    "config below its floor": _with("l3io-wordpress-config", "0.1.6"),
    # Attacks the scope: boto3 is two hops from the extra (aws -> database[aws]).
    "boto3 in the base": _with("boto3", "1.35.0"),
    "the otel sdk in the base": _with("opentelemetry-sdk", "1.40.0"),
    "no extra provided at all": _no_extras,
}


@pytest.mark.parametrize("plant", sorted(TOOL_PLANTS))
def test_the_base_install_tool_reports_a_planted_violation(plant: str) -> None:
    world = copy.deepcopy(WORLD)
    TOOL_PLANTS[plant](world)
    assert _run(world) != []
