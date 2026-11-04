"""AD-20 — an Archive becomes visible only when it is complete (AG-21).

:class:`LocalArchiveStore` implements ``ArchiveStoreQuery`` and
``ArchiveStoreCommand`` for ``file:`` targets. :meth:`~LocalArchiveStore.stage`
creates a temporary file **in the destination directory** — same filesystem,
so the rename is atomic — through stdlib :func:`tempfile.mkstemp` (``O_CREAT |
O_EXCL``, mode ``0600``). :meth:`_LocalStagedArchive.publish` runs the injected
verifier once, inside ``phase("VERIFY")``, and only on a pass, and only if the
final name is still free, renames the file into place with :func:`os.replace`
inside ``phase("PUBLISH")``. Any failure discards the staged file; nothing is
ever left at the final name. The verifier has no default, so a store that skips
verification cannot be built by omission.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Final, cast

import structlog

from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.errors import ArchiveExistsError, ArchiveWriteError
from l3io.wp.backup.domain.storage_target import StorageTarget

__all__ = ["LocalArchiveStore"]

_log = structlog.get_logger(__name__)

_STAGED_SUFFIX: Final = ".partial"
_DIR_MODE: Final = 0o700


def _final_path(target: StorageTarget) -> Path:
    return Path(target.path)


def _occupied(path: Path) -> bool:
    """Whether anything is at ``path``, a dangling symlink included."""
    return path.is_symlink() or path.exists()


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


class _DestinationWriter(io.RawIOBase):
    """The staged file. An ``OSError`` (``ENOSPC`` included) names the Archive."""

    def __init__(self, fd: int, final: Path) -> None:
        super().__init__()
        self._file = os.fdopen(fd, "wb")
        self._final = final

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        try:
            return self._file.write(data)
        except OSError as exc:
            raise ArchiveWriteError(self._final, _reason(exc)) from exc

    def sync(self) -> None:
        """Flush and ``fsync``, so a verifier reads what will be published."""
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as exc:
            raise ArchiveWriteError(self._final, _reason(exc)) from exc

    def close(self) -> None:
        if not self.closed:
            try:
                self._file.close()
            finally:
                super().close()


class _LocalStagedArchive:
    """One staged Archive. ``publish`` and ``discard`` each act at most once."""

    def __init__(
        self, final: Path, staged: Path, fd: int, verify: Callable[[Path], None]
    ) -> None:
        self._final = final
        self._staged = staged
        self._stream = _DestinationWriter(fd, final)
        self._verify = verify
        self._done = False

    @property
    def stream(self) -> IO[bytes]:
        """Where the Archive's bytes are written."""
        return cast("IO[bytes]", self._stream)

    @property
    def staged_path(self) -> Path:
        """The temporary name, beside the final one."""
        return self._staged

    def _close(self) -> int:
        """Flush, fsync and close the staged file; its size."""
        if not self._stream.closed:
            self._stream.sync()
            self._stream.close()
        return self._staged.stat().st_size

    def _rename(self) -> int:
        """``os.replace`` onto the final name, refusing one that now exists."""
        if _occupied(self._final):
            raise ArchiveExistsError(self._final)
        size = self._staged.stat().st_size
        try:
            self._staged.replace(self._final)
        except OSError as exc:
            raise ArchiveWriteError(self._final, _reason(exc)) from exc
        return size

    def publish(self) -> bool:
        """Verify the staged file, then rename it onto the final name."""
        if self._done:
            msg = "this staged archive was already published or discarded"
            raise ArchiveWriteError(self._final, msg)
        try:
            with phase(BackupStep.VERIFY.value) as record:
                record.bytes = self._close()
                self._verify(self._staged)
            with phase(BackupStep.PUBLISH.value) as record:
                record.bytes = self._rename()
        except BaseException:
            self.discard()
            raise
        self._done = True
        _log.info("archive_published", path=str(self._final))
        return True

    def discard(self) -> None:
        """Remove the staged file; nothing becomes visible. Idempotent."""
        if self._done:
            return
        self._done = True
        with contextlib.suppress(OSError):
            self._stream.close()
        self._staged.unlink(missing_ok=True)
        _log.info("archive_discarded", path=str(self._final))


class LocalArchiveStore:
    """``ArchiveStoreQuery`` and ``ArchiveStoreCommand`` for local paths."""

    __slots__ = ("_create_directory", "_verify")

    def __init__(
        self, *, verify: Callable[[Path], None], create_directory: bool = False
    ) -> None:
        self._verify = verify
        self._create_directory = create_directory

    def exists(self, target: StorageTarget) -> bool:
        """Whether anything is at the final name (a dangling link included)."""
        return _occupied(_final_path(target))

    def stage(self, target: StorageTarget) -> _LocalStagedArchive:
        """Create the temporary file beside the final name, mode ``0600``.

        With ``create_directory``, a missing destination directory is created
        first (mode ``0700``); a failure is ``ArchiveWriteError``.
        """
        final = _final_path(target)
        directory = final.parent
        try:
            if self._create_directory:
                directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
            fd, staged = tempfile.mkstemp(
                prefix=f".{final.name}.", suffix=_STAGED_SUFFIX, dir=directory
            )
        except OSError as exc:
            raise ArchiveWriteError(final, _reason(exc)) from exc
        _log.debug("archive_staged", path=str(final), staged=staged)
        return _LocalStagedArchive(final, Path(staged), fd, self._verify)
