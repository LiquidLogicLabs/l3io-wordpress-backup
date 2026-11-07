"""AD-14 — every package error survives pickle and ``copy``, with its fields.

A host that runs ``backup``/``restore`` in a ``ProcessPoolExecutor``, Celery or
``multiprocessing`` receives the exception pickled. ``BaseException`` rebuilds
with ``cls(*args)`` and ``args`` is the one formatted message, so a class whose
constructor takes fields could not be rebuilt: the pool broke and the real
error was lost (E006 closure AR-1/RT-4).

The set is derived from the tree (``tests.exception_scope``), never listed.
Each class is rebuilt without calling its constructor (its fields are not
known generically), so what is checked is the mechanism every class inherits;
the named classes below are built through their real constructors.
"""

from __future__ import annotations

import copy
import pickle
from concurrent.futures import ProcessPoolExecutor
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.api import DumpFailedError, SiteLockedError, UnexpectedError
from l3io.wp.backup.domain.errors import BackupError, UsageError
from tests.exception_scope import (
    PACKAGE,
    PACKAGE_DIR,
    SRC,
    error_classes,
    import_all,
    module_names,
)


def _all_classes() -> list[type[BackupError]]:
    import_all(module_names(SRC, PACKAGE_DIR))
    found = sorted(error_classes(PACKAGE), key=lambda c: f"{c.__module__}.{c.__name__}")
    return [c for c in found if issubclass(c, BackupError)]


def test_the_walk_is_not_vacuous() -> None:
    assert len(_all_classes()) > 60


@pytest.mark.parametrize("cls", _all_classes(), ids=lambda c: c.__name__)
def test_every_error_class_round_trips_through_pickle(cls: type[BackupError]) -> None:
    if cls.__module__.startswith("tests") or cls.__name__.startswith("_"):
        pytest.skip("not a public class")
    error = cls.__new__(cls)
    Exception.__init__(error, "a formatted message")
    error.snapshot_path = PurePosixPath("/snap/x")
    error.field_marker = "kept"  # type: ignore[attr-defined]
    for rebuilt in (pickle.loads(pickle.dumps(error)), copy.copy(error)):  # noqa: S301
        assert type(rebuilt) is cls
        assert str(rebuilt) == "a formatted message"
        assert rebuilt.snapshot_path == PurePosixPath("/snap/x")
        assert rebuilt.field_marker == "kept"  # type: ignore[attr-defined]


def test_constructed_errors_keep_their_fields_through_pickle() -> None:
    usage = UsageError("bad", arguments=("passphrase", "encrypt"))
    unexpected = UnexpectedError("tarfile.ReadError", api_function="backup")
    locked = SiteLockedError(PurePosixPath("/srv/site"), PurePosixPath("/srv/lock"))
    for original in (usage, unexpected, locked):
        rebuilt = pickle.loads(pickle.dumps(original))  # noqa: S301
        assert type(rebuilt) is type(original)
        assert str(rebuilt) == str(original)
        assert rebuilt.__dict__ == original.__dict__
    assert pickle.loads(pickle.dumps(usage)).arguments == ("passphrase", "encrypt")  # noqa: S301


def _raise_dump_failed() -> None:
    raise DumpFailedError("dump failed", database="db_x", exit_status=2)  # noqa: EM101, TRY003


def test_a_process_pool_delivers_the_real_error() -> None:
    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_raise_dump_failed)
        with pytest.raises(DumpFailedError) as caught:
            future.result()
    assert caught.value.database == "db_x"
    assert caught.value.exit_status == 2
