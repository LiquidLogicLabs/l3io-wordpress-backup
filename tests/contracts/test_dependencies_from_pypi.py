"""Every dependency resolves from PyPI, the family included (2026-11-10).

Until 2026-11-10 the family (``l3io-wordpress-database``, ``-config`` and the
``l3io-age-passphrase`` binder) came from TestPyPI through ``[tool.uv.sources]``
and an explicit ``[[tool.uv.index]]``. A release resolves from PyPI alone, so a
development path that resolves differently checks something an operator never
installs. These tests hold the dependency path to PyPI:

- ``pyproject.toml`` declares no ``[tool.uv.sources]`` and no extra index;
- every package in ``uv.lock`` -- the resolved set, so transitive packages are
  covered without a list -- comes from ``https://pypi.org/simple``, except the
  project itself;
- no install recipe (workflows, Makefile, Dockerfiles, shell scripts) names the
  TestPyPI simple index. Publishing backup itself to TestPyPI (publish.yml's
  ``testpypi`` target, ``/legacy/``) is not the dependency path and is allowed.

Each rule is proven by planted violations, including ones that attack its scope.
"""

from __future__ import annotations

import copy
import pathlib
import tomllib
from collections.abc import Callable
from typing import Any

import pytest
from packaging.requirements import Requirement

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPI = "https://pypi.org/simple"
TESTPYPI_SIMPLE = "test.pypi.org/simple"


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _family(pyproject: dict[str, Any]) -> set[str]:
    """The family packages the project names, read from its own requirements."""
    project = pyproject["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    names = {Requirement(r).name for r in reqs}
    return {n for n in names if n.startswith("l3io-")}


def _findings(pyproject: dict[str, Any], lock: dict[str, Any]) -> list[str]:
    found: list[str] = []
    uv = pyproject.get("tool", {}).get("uv", {})
    for key in ("sources", "index", "index-url", "extra-index-url", "find-links"):
        if key in uv:
            found.append(f"pyproject.toml: [tool.uv] declares {key!r}")
    own = pyproject["project"]["name"]
    packages = lock.get("package", [])
    names = {p["name"] for p in packages}
    for name in sorted(_family(pyproject) - names):
        found.append(f"uv.lock: family package {name} is not locked")
    for pkg in packages:
        name, source = pkg["name"], pkg.get("source")
        if name == own and source in ({"editable": "."}, {"virtual": "."}):
            continue
        if source != {"registry": PYPI}:
            found.append(f"uv.lock: {name} resolves from {source!r}, not {PYPI}")
    return found


def test_the_real_dependency_path_is_pypi_only() -> None:
    pyproject, lock = _pyproject(), _lock()
    assert _family(pyproject), "no family package found; the check would be vacuous"
    assert len(lock.get("package", [])) > len(_family(pyproject)), "uv.lock looks empty"
    assert _findings(pyproject, lock) == []


def _set(path: tuple[str, ...], value: object) -> Callable[[dict, dict], None]:
    def plant(pyproject: dict, _lock: dict) -> None:
        node = pyproject
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    return plant


def _relock(name: str, source: object) -> Callable[[dict, dict], None]:
    def plant(_pyproject: dict, lock: dict) -> None:
        for pkg in lock["package"]:
            if pkg["name"] == name:
                if source is None:
                    del pkg["source"]
                else:
                    pkg["source"] = source

    return plant


def _unlock(name: str) -> Callable[[dict, dict], None]:
    def plant(_pyproject: dict, lock: dict) -> None:
        lock["package"] = [p for p in lock["package"] if p["name"] != name]

    return plant


def _add_family_extra(_pyproject: dict, _lock: dict) -> None:
    _pyproject["project"]["optional-dependencies"]["planted"] = ["l3io-planted>=1"]


PLANTS = {
    "a testpypi source": _set(
        ("tool", "uv", "sources"), {"l3io-wordpress-database": {"index": "testpypi"}}
    ),
    "an explicit testpypi index": _set(
        ("tool", "uv", "index"),
        [
            {
                "name": "testpypi",
                "url": "https://test.pypi.org/simple/",
                "explicit": True,
            }
        ],
    ),
    "an extra index url": _set(
        ("tool", "uv", "extra-index-url"), ["https://test.pypi.org/simple/"]
    ),
    "a family package locked from testpypi": _relock(
        "l3io-age-passphrase", {"registry": "https://test.pypi.org/simple/"}
    ),
    "a transitive package from git": _relock(
        "pyrage", {"git": "https://github.com/woodruffw/pyrage?rev=x"}
    ),
    "a package with no source": _relock("l3io-wordpress-config", None),
    # Scope attacks: the set examined must not shrink silently.
    "a family package missing from the lock": _unlock("l3io-wordpress-database"),
    "a new family requirement never locked": _add_family_extra,
}


@pytest.mark.parametrize("plant", PLANTS.values(), ids=PLANTS.keys())
def test_a_planted_violation_is_reported(plant: Callable[[dict, dict], None]) -> None:
    pyproject, lock = copy.deepcopy(_pyproject()), copy.deepcopy(_lock())
    assert _findings(pyproject, lock) == [], "the real tree must be clean first"
    plant(pyproject, lock)
    assert _findings(pyproject, lock), "the planted violation was not reported"


def _install_recipes() -> list[pathlib.Path]:
    """Files that install dependencies, found by pattern, never by a list."""
    patterns = (".github/workflows/*.yml", "Makefile", "**/Dockerfile*", "**/*.sh")
    skip = {".git", ".venv", "node_modules"}
    found = {
        p
        for pattern in patterns
        for p in ROOT.glob(pattern)
        if p.is_file() and not skip.intersection(p.relative_to(ROOT).parts)
    }
    return sorted(found)


def test_there_are_install_recipes_to_check() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _install_recipes()}
    assert ".github/workflows/ci.yml" in names
    assert "tests/sidecar/Dockerfile" in names


@pytest.mark.parametrize(
    "recipe", _install_recipes(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_no_install_recipe_names_the_testpypi_index(recipe: pathlib.Path) -> None:
    text = recipe.read_text(encoding="utf-8", errors="replace")
    assert TESTPYPI_SIMPLE not in text, (
        f"{recipe.relative_to(ROOT)}: names {TESTPYPI_SIMPLE}; dependencies resolve "
        "from PyPI only"
    )
