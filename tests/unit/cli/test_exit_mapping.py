"""FR-21 — every package error maps to a code by its class attribute.

The error classes are found by importing every package module and walking
``BackupError.__subclasses__()`` recursively, so a class added anywhere is in
scope without an edit here. The source guards scan every ``*.py`` under the
package directory (``rglob``), and each guard is proved by planting a
violation that attacks its scope: a file in a new sub-directory, and an error
class that did not exist when the test module was written.
"""

from __future__ import annotations

import argparse
import ast
import gc
import importlib
import pkgutil
from pathlib import Path

import pytest

import l3io.wp.backup as package
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.domain import errors
from l3io.wp.backup.domain.errors import BackupError, RefusedError, exit_code_of
from l3io.wp.backup.domain.exit_codes import ExitCode

PACKAGE_DIR = Path(package.__file__).parent
CLI_DIR = PACKAGE_DIR / "cli"
SOURCE_OF_TRUTH = PACKAGE_DIR / "domain" / "exit_codes.py"


def _import_every_module() -> list[str]:
    names = [
        info.name
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
    ]
    for name in names:
        importlib.import_module(name)
    return names


def _walk(base: type[BackupError]) -> list[type[BackupError]]:
    found: list[type[BackupError]] = []
    pending = list(base.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls not in found:
            found.append(cls)
            pending.extend(cls.__subclasses__())
    return found


_import_every_module()
ERROR_CLASSES = sorted(_walk(BackupError), key=lambda c: c.__qualname__)


def test_the_walk_found_the_package_errors() -> None:
    names = {cls.__name__ for cls in ERROR_CLASSES}
    assert set(errors.__all__) - {"BackupError", "exit_code_of"} <= names


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=lambda c: c.__qualname__)
def test_every_error_class_exits_non_zero(cls: type[BackupError]) -> None:
    assert isinstance(cls.exit_code, ExitCode)
    assert cls.exit_code is not ExitCode.OK


REFUSALS = [cls for cls in ERROR_CLASSES if issubclass(cls, RefusedError)]


def test_the_refusal_walk_is_not_empty() -> None:
    names = {cls.__name__ for cls in REFUSALS}
    assert {"ArchiveExistsError", "UnsupportedStorageSchemeError"} <= names


@pytest.mark.parametrize("cls", REFUSALS, ids=lambda c: c.__qualname__)
def test_every_refusal_exits_refused(cls: type[BackupError]) -> None:
    assert cls.exit_code is ExitCode.REFUSED


#: The AC's override table — the expectation, not the scope: the scope is the
#: walk, and a class declaring ``exit_code`` that is not here fails.
DECLARED = {
    "BackupError": ExitCode.FAILED,
    "RefusedError": ExitCode.REFUSED,
    "UsageError": ExitCode.USAGE,
    "CredentialsError": ExitCode.CREDENTIALS,
    "DatabaseUnreachableError": ExitCode.UNREACHABLE,
    "VerificationFailedError": ExitCode.VERIFICATION_FAILED,
    "UnrecognisedArchiveError": ExitCode.VERIFICATION_FAILED,
    "UnsupportedFormatVersionError": ExitCode.VERIFICATION_FAILED,
    # verify --authenticate (BL-E002-060): the payload could not be authenticated.
    "PassphraseRejectedError": ExitCode.VERIFICATION_FAILED,
    "PayloadDamagedError": ExitCode.VERIFICATION_FAILED,
    # E003-S01-001 (red team RT-8): the Archive changed between probe and VERIFY.
    "ArchiveChangedError": ExitCode.VERIFICATION_FAILED,
    "SiteLockedError": ExitCode.LOCKED,
    # E002-S02-008: an unusable lock directory; the Run does not proceed unlocked.
    "SiteLockLocationError": ExitCode.NOT_READY,
    "MissingExtraError": ExitCode.NOT_READY,
    "PreflightFailedError": ExitCode.NOT_READY,
}


def test_exactly_the_documented_classes_declare_a_code() -> None:
    declaring = {
        cls.__name__: cls.__dict__["exit_code"]
        for cls in [BackupError, *ERROR_CLASSES]
        if "exit_code" in cls.__dict__
    }
    assert declaring == DECLARED


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=lambda c: c.__qualname__)
def test_the_mapping_reads_the_class_attribute(cls: type[BackupError]) -> None:
    exc = cls.__new__(cls)
    assert exit_code_for(exc) is cls.exit_code
    assert exit_code_of(exc) is cls.exit_code


def test_a_non_package_error_is_failed_and_argparse_is_usage() -> None:
    assert exit_code_for(ValueError("x")) is ExitCode.FAILED
    assert exit_code_for(KeyboardInterrupt()) is ExitCode.FAILED
    assert exit_code_for(argparse.ArgumentError(None, "bad")) is ExitCode.USAGE


# --------------------------------------------------------------------------
# Source guards
# --------------------------------------------------------------------------


def _int_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


def literal_exit_codes(root: Path, *, skip: Path | None = None) -> list[str]:
    """``ExitCode(<int>)``, or ``exit_code=<int>``, anywhere under ``root``."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if skip is not None and path == skip:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).rsplit(".", 1)[-1]
            if name == "ExitCode" and any(_int_literal(a) for a in node.args):
                found.append(f"{path.relative_to(root)}:{node.lineno}")
            found.extend(
                f"{path.relative_to(root)}:{node.lineno}"
                for kw in node.keywords
                if kw.arg == "exit_code" and _int_literal(kw.value)
            )
    return found


def class_dispatch(root: Path, error_names: set[str]) -> list[str]:
    """A ``match``, or an ``isinstance`` naming a specific error class."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Match):
                found.append(where)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) == 2
            ):
                named = {
                    ast.unparse(n).rsplit(".", 1)[-1]
                    for n in ast.walk(node.args[1])
                    if isinstance(n, ast.Name | ast.Attribute)
                }
                if named & error_names:
                    found.append(where)
    return sorted(found)


def _specific_error_names() -> set[str]:
    """Every BackupError subclass but the base — re-walked, never cached."""
    return {cls.__name__ for cls in _walk(BackupError)}


def test_no_module_builds_an_exit_code_from_a_literal() -> None:
    assert SOURCE_OF_TRUTH.is_file()
    assert literal_exit_codes(PACKAGE_DIR, skip=SOURCE_OF_TRUTH) == []


def test_no_cli_module_dispatches_over_error_classes() -> None:
    names = _specific_error_names()
    assert "ArchiveExistsError" in names
    assert class_dispatch(CLI_DIR, names) == []


def test_the_literal_guard_sees_a_new_sub_directory(tmp_path: Path) -> None:
    nested = tmp_path / "cli" / "later"
    nested.mkdir(parents=True)
    (nested / "x.py").write_text(
        "a = ExitCode(3)\nb = codes.ExitCode(8)\n"
        "phase(exit_code=1)\nok = ExitCode.OK\n",
        encoding="utf-8",
    )
    assert literal_exit_codes(tmp_path) == [
        "cli/later/x.py:1",
        "cli/later/x.py:2",
        "cli/later/x.py:3",
    ]


def test_the_dispatch_guard_sees_a_class_added_after_it_was_written(
    tmp_path: Path,
) -> None:
    # Defined at test time, so no hand list could name it; dropped afterwards
    # so no other test's walk sees it.
    later = type("LaterRefusalError", (RefusedError,), {})
    try:
        assert "LaterRefusalError" in _specific_error_names()
        names = _specific_error_names()
    finally:
        del later
        gc.collect()
    assert "LaterRefusalError" not in _specific_error_names()
    nested = tmp_path / "later"
    nested.mkdir()
    (nested / "x.py").write_text(
        "def f(e):\n"
        "    if isinstance(e, (OSError, LaterRefusalError)):\n"
        "        return 3\n"
        "    if isinstance(e, BackupError):\n"
        "        return 1\n"
        "    match e:\n"
        "        case _:\n"
        "            return 0\n",
        encoding="utf-8",
    )
    assert class_dispatch(tmp_path, names) == [
        "later/x.py:2",
        "later/x.py:6",
    ]


@pytest.mark.parametrize("cls", [*REFUSALS[:1], errors.DumpFailedError])
def test_a_failing_phase_records_the_errors_code(cls: type[BackupError]) -> None:
    from structlog.testing import capture_logs

    from l3io.wp.backup.adapters.phase import phase

    exc = cls.__new__(cls)
    with capture_logs() as logs, pytest.raises(cls), phase("DUMP"):
        raise exc
    (record,) = [e for e in logs if e["event"] == "phase_completed"]
    assert (record["outcome"], record["exit_code"]) == ("failed", int(cls.exit_code))
