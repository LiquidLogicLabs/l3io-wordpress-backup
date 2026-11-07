"""AD-14, FR-23 — ``l3io.wp.backup.api`` exports exactly the package's errors.

The set is derived, never kept: every ``*.py`` under ``src/l3io/wp/backup/``
is imported and ``BackupError``'s subclasses are followed recursively. That
set must equal the exception classes named in ``api.__all__``, in both
directions, and each export must be the defining class by identity. So a
class added in any epic without an export line in ``api/errors.py`` fails
here, and so does an exported exception that is not a ``BackupError``.

A private class (``_ProbeFailedError``) is not exported. That one cannot
reach a caller is a runtime fact ``public_boundary`` enforces: it wraps any
``BackupError`` this package does not export as ``UnexpectedError``
(``tests/unit/api/test_boundary.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import l3io.wp.backup.api as api
from l3io.wp.backup.api import errors as api_errors
from l3io.wp.backup.domain.errors import (
    BackupError,
    CredentialsError,
    RefusedError,
    SiteLockedError,
    UsageError,
    VerificationFailedError,
)
from tests.exception_scope import (
    PACKAGE,
    PACKAGE_DIR,
    SRC,
    error_classes,
    exported_exceptions,
    import_all,
    is_private,
    module_names,
)

LAYERS = ("domain", "adapters", "cli", "core", "api")


def _difference(
    defined: set[type[BaseException]], exported: dict[str, type]
) -> tuple[list[str], list[str], list[str]]:
    """(defined but not exported, exported but not package errors, not by identity)."""
    public = {c for c in defined if not is_private(c)}
    names = {c.__name__ for c in public}
    exported_classes = set(exported.values())
    missing = sorted(
        f"{c.__module__}.{c.__qualname__}" for c in public if c not in exported_classes
    )
    foreign = sorted(
        name
        for name, obj in exported.items()
        if not (issubclass(obj, BackupError) and name in names)
    )
    not_identical = sorted(
        name for name, obj in exported.items() if name in names and obj not in public
    )
    return missing, foreign, not_identical


@pytest.fixture(scope="module")
def defined() -> set[type[BaseException]]:
    modules = module_names(SRC, PACKAGE_DIR)
    import_all(modules)
    return error_classes(PACKAGE)


def test_the_walk_covers_every_layer_and_epic_two_s_classes(
    defined: set[type[BaseException]],
) -> None:
    """Scope assertion: an empty or partial walk would pass vacuously."""
    modules = module_names(SRC, PACKAGE_DIR)
    for layer in LAYERS:
        assert any(m.startswith(f"{PACKAGE}.{layer}.") for m in modules), layer
    layers_found = {c.__module__.split(".")[3] for c in defined}
    assert layers_found >= {"domain", "adapters", "cli", "core", "api"}
    assert {
        BackupError,
        RefusedError,
        UsageError,
        CredentialsError,
        SiteLockedError,
        VerificationFailedError,
        api.UnexpectedError,
    } <= defined


def test_api_exports_exactly_the_package_errors(
    defined: set[type[BaseException]],
) -> None:
    missing, foreign, not_identical = _difference(
        defined, exported_exceptions(api.__all__, api)
    )
    assert missing == [], f"defined but not exported from api/errors.py: {missing}"
    assert foreign == [], f"exported but not a package BackupError: {foreign}"
    assert not_identical == [], f"exported but not the defining class: {not_identical}"


def test_api_errors_all_is_what_api_re_exports() -> None:
    assert set(api_errors.__all__) <= set(api.__all__)
    for name in api_errors.__all__:
        assert getattr(api, name) is getattr(api_errors, name)


# --------------------------------------------------------------------------
# Planted cases: each attacks the check's scope
# --------------------------------------------------------------------------


def test_a_class_planted_deep_without_an_export_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subclass two levels down, beside no ``__init__.py``, and not exported."""
    package = tmp_path / "plantedpkg"
    (package / "deep" / "deeper").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "deep" / "deeper" / "rogue.py").write_text(
        "from l3io.wp.backup.domain.errors import BackupError\n"
        "class Planted(BackupError):\n"
        "    def __init__(self, path: str) -> None:\n"
        "        self.path = path\n"
        "        super().__init__(path)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        import_all(module_names(tmp_path, package))
        planted = error_classes("plantedpkg")
    finally:
        for name in [m for m in sys.modules if m.startswith("plantedpkg")]:
            del sys.modules[name]
    assert [c.__name__ for c in planted] == ["Planted"]
    missing, _, _ = _difference(planted, {})
    assert missing == ["plantedpkg.deep.deeper.rogue.Planted"]


def test_an_exported_non_package_exception_fails() -> None:
    missing, foreign, _ = _difference(
        {BackupError}, {"BackupError": BackupError, "ValueError": ValueError}
    )
    assert missing == []
    assert foreign == ["ValueError"]


def test_an_export_that_is_not_the_defining_class_fails() -> None:
    class SiteLockedError(BackupError):  # a same-named copy
        pass

    _, foreign, not_identical = _difference(
        {SiteLockedError_ := api.SiteLockedError},
        {"SiteLockedError": SiteLockedError},
    )
    assert SiteLockedError_ is not SiteLockedError
    assert not_identical == ["SiteLockedError"]
    assert foreign == []


def test_a_same_named_class_in_a_second_module_is_missing() -> None:
    """Two public classes share a name; only one is exported (identity, not name)."""

    class SiteLockedError(BackupError):
        pass

    SiteLockedError.__qualname__ = "SiteLockedError"
    SiteLockedError.__module__ = "l3io.wp.backup.adapters.planted"
    missing, _, _ = _difference(
        {api.SiteLockedError, SiteLockedError},
        {"SiteLockedError": api.SiteLockedError},
    )
    assert missing == ["l3io.wp.backup.adapters.planted.SiteLockedError"]
