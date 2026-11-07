"""FR-23 — every exported exception type has a reachable raise site.

An ``ast`` walk over every ``*.py`` under ``src/l3io/wp/backup/`` collects the
names that follow ``raise`` and the names called anywhere outside their own
class body: a class built by a helper (``preflight_failure``) or bound to a
variable before it is raised is reached by its construction (E006 gate, B1).
Every exception class in ``api.__all__`` must appear there, or have a
subclass that does: a grouping base such as ``RefusedError`` is reached
through its subclasses.
"""

from __future__ import annotations

from pathlib import Path

import l3io.wp.backup.api as api
from tests.exception_scope import (
    PACKAGE_DIR,
    construction_sites,
    exported_exceptions,
)

LAYERS = ("domain", "adapters", "cli", "core", "api", "ports")


def _unreached(exported: dict[str, type], reached: set[str]) -> list[str]:
    """Exported classes neither constructed nor with an exported subclass that is."""
    classes = list(exported.values())

    def is_reached(cls: type) -> bool:
        return any(
            issubclass(other, cls) and other.__name__ in reached for other in classes
        )

    return sorted(name for name, cls in exported.items() if not is_reached(cls))


def _files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def test_the_walk_visits_every_layer() -> None:
    """Scope assertion: more than one file in each layer directory."""
    files = _files(PACKAGE_DIR)
    for layer in LAYERS:
        assert sum(1 for f in files if f.parent.name == layer) > 1, layer


def test_every_exported_exception_has_a_raise_site() -> None:
    sites = construction_sites(_files(PACKAGE_DIR), PACKAGE_DIR)
    exported = exported_exceptions(api.__all__, api)
    assert len(exported) > 1
    assert _unreached(exported, set(sites)) == []


def test_unexpected_error_is_reached_through_the_boundary() -> None:
    sites = construction_sites(_files(PACKAGE_DIR), PACKAGE_DIR)
    assert [s.split(":")[0] for s in sites["UnexpectedError"]] == ["api/errors.py"]


# --------------------------------------------------------------------------
# Planted cases
# --------------------------------------------------------------------------


def _plant(tmp_path: Path, files: dict[str, str]) -> dict[str, list[str]]:
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return construction_sites(_files(tmp_path), tmp_path)


def test_a_class_nothing_raises_or_builds_fails(tmp_path: Path) -> None:
    class Base(Exception):
        pass

    class Raised(Base):
        pass

    class Orphan(Base):
        pass

    sites = _plant(
        tmp_path,
        {
            "deep/deeper/raiser.py": "raise Raised('x')\n",
            # Orphan builds itself in its own body only: that reaches nothing.
            "errors.py": (
                "class Orphan(Base):\n"
                "    @classmethod\n"
                "    def make(cls):\n"
                "        return Orphan()\n"
            ),
        },
    )
    exported = {"Base": Base, "Raised": Raised, "Orphan": Orphan}
    assert _unreached(exported, set(sites)) == ["Orphan"]


def test_a_construction_deep_in_a_helper_counts(tmp_path: Path) -> None:
    """A class returned by a helper and raised through a variable is reached."""
    sites = _plant(
        tmp_path,
        {
            "a/b/c/helper.py": (
                "def failure(tool):\n    return Built(tool)\n"
                "def run():\n    exc = failure('wp')\n    raise exc\n"
            ),
            "a/bare.py": "raise Bare from None\n",
            "a/qualified.py": "raise errors.Qualified('x') from None\n",
        },
    )
    assert {"Built", "Bare", "Qualified"} <= set(sites)
    assert sites["Built"] == ["a/b/c/helper.py:2"]
