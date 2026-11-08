"""AD-14 — ``public_boundary``: package errors pass, everything else is wrapped.

Each failure is raised from a driver double, past every adapter's own wrap,
so the boundary is the only translation left. The parametrisation walks
``l3io.wp.database.errors`` for every exception class it defines, so a class
database adds later is covered without an edit here.
"""

from __future__ import annotations

import functools
import inspect
import json
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import l3io.wp.backup.api as api
from l3io.wp.backup.api import errors as api_errors
from l3io.wp.backup.api import operations
from l3io.wp.backup.cli.exit import RUN_ID_ATTR
from l3io.wp.backup.domain.errors import (
    BackupError,
    CredentialsError,
    DatabaseRefusedError,
    SiteLockedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)

wpdb_errors = pytest.importorskip("l3io.wp.database.errors")


def _database_errors() -> list[type[BaseException]]:
    found = [
        obj
        for _, obj in inspect.getmembers(wpdb_errors, inspect.isclass)
        if issubclass(obj, BaseException) and obj.__module__ == wpdb_errors.__name__
    ]
    assert len(found) > 5, "the walk of l3io.wp.database.errors found too little"
    return found


def _instance(cls: type[BaseException]) -> BaseException:
    """``cls`` raised as a dependency would, without knowing its signature."""
    exc = cls.__new__(cls)
    BaseException.__init__(exc, "dependency text")
    return exc


def _json_error() -> BaseException:
    try:
        json.loads("{")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError


FOREIGN: list[Callable[[], BaseException]] = [
    *(lambda c=c: _instance(c) for c in _database_errors()),
    lambda: OSError(28, "No space left on device"),
    lambda: tarfile.ReadError("not a gzip file"),
    _json_error,
    lambda: ValueError("dependency text"),
    lambda: ExceptionGroup("group", [OSError("one"), ValueError("two")]),
]


def _call(
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    raised: BaseException,
    tmp_path: Path,
    site: Path,
) -> None:
    driver = {"backup": "drive_backup", "verify": "drive_verify"}.get(
        entry, "drive_restore"
    )

    def double(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        raise raised

    monkeypatch.setattr(operations, driver, double)
    archive = tmp_path / "a.tar.gz"
    if entry == "backup":
        api.backup(site, scope=api.BackupScope.FILES, target=archive)
    elif entry == "verify":
        api.verify(archive)
    else:
        api.restore(
            site,
            archive=archive,
            mode=RestoreMode.FILES,
            overrides=OverrideSet.none(),
            snapshot=SnapshotPolicy.SKIP,
            config_policy=ConfigPolicy.KEEP,
        )


def _qualified(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


@pytest.mark.parametrize(
    "make", FOREIGN, ids=lambda f: _qualified(f()).rsplit(".", 1)[-1]
)
def test_a_foreign_exception_arrives_as_unexpected_error(
    make: Callable[[], BaseException],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    site: Path,
) -> None:
    original = make()
    with capture_logs() as logs, pytest.raises(api.UnexpectedError) as caught:
        _call(monkeypatch, "backup", original, tmp_path, site)
    exc = caught.value
    assert exc.__cause__ is original
    assert exc.__suppress_context__
    assert exc.cause_type == _qualified(original)
    assert exc.api_function == "backup"
    assert exc.exit_code is ExitCode.FAILED
    assert "dependency text" not in str(exc)
    assert str(original) not in str(exc)
    records = [e for e in logs if e["event"] == "unexpected_exception"]
    assert len(records) == 1
    assert records[0]["log_level"] == "error"
    assert records[0]["cause_type"] == _qualified(original)
    assert records[0]["api_function"] == "backup"
    assert "exc_info" not in records[0]
    assert "l3io_run_id" not in records[0]


def test_an_exception_group_keeps_its_members_on_the_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: Path
) -> None:
    members = [OSError("one"), ValueError("two")]
    with capture_logs(), pytest.raises(api.UnexpectedError) as caught:
        _call(monkeypatch, "backup", ExceptionGroup("g", members), tmp_path, site)
    cause = caught.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert list(cause.exceptions) == members


@pytest.mark.parametrize("entry", ["backup", "verify", "restore"])
def test_every_entry_point_wraps_and_carries_the_stamped_run_id(
    entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: Path
) -> None:
    original = OSError("disk")
    setattr(original, RUN_ID_ATTR, "run-123")
    with capture_logs() as logs, pytest.raises(api.UnexpectedError) as caught:
        _call(monkeypatch, entry, original, tmp_path, site)
    assert caught.value.api_function == entry
    [record] = [e for e in logs if e["event"] == "unexpected_exception"]
    assert record["l3io_run_id"] == "run-123"
    assert record["api_function"] == entry


@pytest.mark.parametrize(
    "error",
    [
        CredentialsError("wp-config"),
        DatabaseRefusedError(("database_not_empty",)),
        SiteLockedError(Path("/srv/site"), Path("/state/x.lock")),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_backup_error_passes_through_by_identity(
    error: BackupError, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: Path
) -> None:
    """The adapter's mapping (database's error to a package one) reaches the
    caller as that mapped type, with its own cause, and is logged by nothing."""
    database_cause = _instance(wpdb_errors.AuthenticationFailedError)
    error.__cause__ = database_cause
    with capture_logs() as logs, pytest.raises(BackupError) as caught:
        _call(monkeypatch, "backup", error, tmp_path, site)
    assert caught.value is error
    assert caught.value.__cause__ is database_cause
    assert not [e for e in logs if e["event"] == "unexpected_exception"]


@pytest.mark.parametrize("make", [KeyboardInterrupt, lambda: SystemExit(3)])
def test_a_base_exception_that_is_not_an_exception_propagates(
    make: Callable[[], BaseException],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    site: Path,
) -> None:
    original = make()
    with capture_logs() as logs, pytest.raises(type(original)) as caught:
        _call(monkeypatch, "backup", original, tmp_path, site)
    assert caught.value is original
    assert not [e for e in logs if e["event"] == "unexpected_exception"]


def _entry_points() -> list[str]:
    return [
        name
        for name in api.__all__
        if inspect.isfunction(obj := getattr(api, name))
        and getattr(inspect.unwrap(obj), "__module__", None) == operations.__name__
    ]


def _unwrapped(entries: list[str]) -> list[str]:
    return [
        n
        for n in entries
        if not hasattr(getattr(api, n), "__wrapped__")
        or not getattr(getattr(api, n), api_errors.BOUNDARY_MARK, False)
    ]


def test_every_entry_point_in_all_is_wrapped() -> None:
    entries = _entry_points()
    assert {"backup", "verify", "restore"} <= set(entries)
    assert _unwrapped(entries) == []


def test_an_unwrapped_entry_point_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planted: an undecorated function defined in operations, exported."""

    def prune() -> None:
        pass

    prune.__module__ = operations.__name__
    monkeypatch.setattr(api, "prune", prune, raising=False)
    monkeypatch.setattr(api, "__all__", [*api.__all__, "prune"])
    entries = _entry_points()
    assert "prune" in entries
    assert _unwrapped(entries) == ["prune"]


def test_another_wraps_decorator_is_not_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: ``functools.wraps`` alone sets ``__wrapped__`` but not the mark."""

    def prune() -> None:
        pass

    @functools.wraps(prune)
    def traced() -> None:
        prune()

    prune.__module__ = traced.__module__ = operations.__name__
    monkeypatch.setattr(api, "prune", traced, raising=False)
    monkeypatch.setattr(api, "__all__", [*api.__all__, "prune"])
    assert _unwrapped(_entry_points()) == ["prune"]


def test_an_unexported_package_error_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: Path
) -> None:
    """A private ``BackupError`` (as ``_ProbeFailedError`` is) never reaches a
    caller as itself: no undocumented type crosses the boundary."""

    class _Private(BackupError):
        def __init__(self, tool: str) -> None:
            self.tool = tool
            super().__init__(f"{tool} failed")

    original = _Private("wp")
    with capture_logs() as logs, pytest.raises(api.UnexpectedError) as caught:
        _call(monkeypatch, "backup", original, tmp_path, site)
    assert caught.value.__cause__ is original
    assert caught.value.cause_type.endswith("._Private")
    assert [e["event"] for e in logs].count("unexpected_exception") == 1


def test_the_boundary_wraps_a_plain_function_and_keeps_its_signature() -> None:
    @api_errors.public_boundary
    def entry(x: int, *, y: str = "a") -> str:
        """Doc."""
        raise LookupError(x)

    assert entry.__name__ == "entry"
    assert entry.__doc__ == "Doc."
    assert str(inspect.signature(entry)) == "(x: 'int', *, y: 'str' = 'a') -> 'str'"
    with capture_logs(), pytest.raises(api.UnexpectedError) as caught:
        entry(1)
    assert caught.value.cause_type == "builtins.LookupError"


def test_the_unexpected_record_carries_frames_and_never_the_original_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: Path
) -> None:
    """The Run's registry is closed when this record is written, so it cannot redact.

    A dependency's exception text (here a unique sentinel standing for a
    password the driver echoed) must not reach the host's log, in the message
    or in the formatted traceback; the frames and the class name must.
    """
    sentinel = "SENTINEL-E006-CLOSURE-8c41d7f2"
    original = RuntimeError(f"driver echoed password={sentinel}")
    with capture_logs() as logs, pytest.raises(api.UnexpectedError) as caught:
        _call(monkeypatch, "backup", original, tmp_path, site)
    [record] = [e for e in logs if e["event"] == "unexpected_exception"]
    assert sentinel not in repr(record)
    assert sentinel not in str(caught.value)
    assert "RuntimeError" in record["traceback"]
    assert "Traceback (most recent call last)" in record["traceback"]
    assert caught.value.__cause__ is original
