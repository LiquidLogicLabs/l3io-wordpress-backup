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


#: A refusal that declares another code (owner decision 2026-11-07, item 9):
#: a missing passphrase is a missing key, CREDENTIALS.
NOT_REFUSED = frozenset({"PassphraseRequiredError"})
REFUSALS = [
    cls
    for cls in ERROR_CLASSES
    if issubclass(cls, RefusedError) and cls.__name__ not in NOT_REFUSED
]


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
    # Owner decision 76: a required dependency missing from a broken install.
    "IncompleteInstallError": ExitCode.NOT_READY,
    "LoadReadinessError": ExitCode.NOT_READY,
    "PreflightFailedError": ExitCode.NOT_READY,
    # Owner decision 2026-11-07 (item 9: BL-E002-069/074/103).
    "PassphraseRequiredError": ExitCode.CREDENTIALS,
    "ArchiveNotFoundError": ExitCode.USAGE,
    "SiteRootNotFoundError": ExitCode.USAGE,
    "ExtractionRefusedAfterCleanError": ExitCode.VERIFICATION_FAILED,
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


#: Calls whose integer argument is a process exit status.
_EXITING_CALLS = frozenset({"ExitCode", "SystemExit", "exit"})
#: Return annotations that make ``return <int>`` an exit status under ``cli/``.
_EXIT_RETURNS = frozenset({"int", "ExitCode"})


def _assigned_exit_codes(node: ast.AST) -> bool:
    """``exit_code = <int>``, ``exit_code: T = <int>``, or a tuple target."""
    if isinstance(node, ast.AnnAssign):
        pairs = [(node.target, node.value)]
    elif isinstance(node, ast.Assign):
        pairs = []
        for target in node.targets:
            if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                pairs.extend(zip(target.elts, node.value.elts, strict=False))
            else:
                pairs.append((target, node.value))
    else:
        return False
    return any(
        isinstance(target, ast.Name | ast.Attribute)
        and ast.unparse(target).rsplit(".", 1)[-1] == "exit_code"
        and value is not None
        and _int_literal(value)
        for target, value in pairs
    )


def _literal_returns(tree: ast.AST) -> list[int]:
    """``return <int>`` in a function annotated ``-> int`` or ``-> ExitCode``."""
    lines: list[int] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if func.returns is None:
            continue
        if ast.unparse(func.returns).rsplit(".", 1)[-1] not in _EXIT_RETURNS:
            continue
        lines.extend(
            node.lineno
            for node in ast.walk(func)
            if isinstance(node, ast.Return)
            and node.value is not None
            and _int_literal(node.value)
        )
    return lines


def literal_exit_codes(root: Path, *, skip: Path | None = None) -> list[str]:
    """An exit status built from an integer literal, anywhere under ``root``.

    ``ExitCode(<int>)``, ``SystemExit(<int>)``, ``sys.exit(<int>)``,
    ``exit_code=<int>`` keywords, ``exit_code`` assignments (tuple targets
    included), and, under ``cli/``, ``return <int>`` from a function annotated
    ``-> int`` or ``-> ExitCode``.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if skip is not None and path == skip:
            continue
        relative = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines: set[int] = set()
        for node in ast.walk(tree):
            if _assigned_exit_codes(node):
                lines.add(node.lineno)
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).rsplit(".", 1)[-1]
            if name in _EXITING_CALLS and any(_int_literal(a) for a in node.args):
                lines.add(node.lineno)
            lines.update(
                node.lineno
                for kw in node.keywords
                if kw.arg == "exit_code" and _int_literal(kw.value)
            )
        if relative.parts[:1] == ("cli",) or root.name == "cli":
            lines.update(_literal_returns(tree))
        found.extend(f"{relative}:{line}" for line in sorted(lines))
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


def test_the_literal_guard_sees_every_shape_the_old_code_used(tmp_path: Path) -> None:
    """F-05: the shapes the pre-ExitCode code used, each on its own line."""
    (tmp_path / "cli").mkdir()
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "phase.py").write_text(
        'outcome, exit_code = "failed", 1\n'
        "self.exit_code = 2\n"
        "exit_code: int = 3\n"
        "ok, exit_code = 'ok', ExitCode.OK\n"
        "def count() -> int:\n    return 4\n",
        encoding="utf-8",
    )
    (tmp_path / "cli" / "main.py").write_text(
        "import sys\n"
        "def main() -> int:\n    return 1\n"
        "def run() -> ExitCode:\n    return 2\n"
        "def width():\n    return 80\n"
        "raise SystemExit(3)\n"
        "sys.exit(8)\n"
        "def fine() -> int:\n    return int(ExitCode.OK)\n",
        encoding="utf-8",
    )
    assert literal_exit_codes(tmp_path) == [
        "adapters/phase.py:1",
        "adapters/phase.py:2",
        "adapters/phase.py:3",
        "cli/main.py:3",
        "cli/main.py:5",
        "cli/main.py:8",
        "cli/main.py:9",
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
