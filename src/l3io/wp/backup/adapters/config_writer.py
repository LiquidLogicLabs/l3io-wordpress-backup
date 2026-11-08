"""AD-18.2/AD-18.4 — the one writer of ``wp-config.php`` (ADR-0010, ADR-0023).

:class:`WpConfigWriter` implements ``ConfigQuery`` and ``ConfigCommand`` over
``l3io-wordpress-config`` (the ``database`` extra, imported inside the
functions that use it — AD-8). This is the only module in the package that
imports ``l3io.wp.config``; :func:`read_defined` is the reader
``adapters/database.py`` uses for ``WP_SITEURL``/``WP_HOME``.

``set_constants`` and ``replace_file`` write no config bytes themselves: each
key is one ``WpConfigFile.set``, and the ``--overwrite-wp-config`` replacement
(the Archive copy's text, handed over in memory, with ``keep`` applied through
``WpConfigString.set``) is one ``WpConfigFile.replace_content`` — the library's
atomic, read-back-verified write that keeps the file's mode and owner and
fsyncs the file and its directory (0.1.4, BL-E002-091). The module's one own
write is ``save_copy`` (a ``0600`` fsynced copy of the target's config), which
that package has no call for. Nothing translates a newline, so CRLF stays CRLF
byte for byte.

Every ``DB_PASSWORD`` value this module is handed or reads is registered with
the Run's ``SecretRegistry`` before anything else happens to it; records name
constant **keys**, never values.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.workspace import private_directories
from l3io.wp.backup.domain.config import ConfigWriteResult, ConstantValue
from l3io.wp.backup.domain.errors import (
    ConfigWriteFailedError,
    CredentialsError,
    MissingExtraError,
)
from l3io.wp.backup.ports.config import WHOLE_FILE

__all__ = ["WpConfigWriter", "read_defined", "refuse_unkeepable"]

_log = structlog.get_logger(__name__)

_EXTRA: Final = "database"
_NS_PER_MS: Final = 1_000_000
_PASSWORD_KEY: Final = "DB_PASSWORD"  # noqa: S105 - a constant name
_TMP_SUFFIX: Final = ".l3io-tmp"
#: The text ``l3io-wordpress-config`` raises with when the owner cannot be kept.
_OWNER_MARKER: Final = "cannot keep the file's owner"
_OWNER_REASON: Final = (
    "the file's owner cannot be kept (run as that owner, or as root); "
    "the file was not changed"
)
_READ_BACK_REASON: Final = "read-back verification failed; the file was not changed"
_ABSENT_REASON: Final = "wp-config.php does not exist, and is never created here"


def _require_extra() -> None:
    try:
        import l3io.wp.config  # noqa: F401 - imported for its presence only
    except ImportError as exc:
        raise MissingExtraError(extra=_EXTRA) from exc


def _constant(value: object) -> ConstantValue:
    """The library's value as a ``ConstantValue``; a float as its literal text."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)


def read_defined(
    config_file: Path, keys: Sequence[str], *, label: str
) -> dict[str, ConstantValue]:
    """``keys``' values in ``config_file``; ``None`` for an undefined one.

    An unreadable file is ``CredentialsError(label)``: ``wp-config.php`` is
    the credential provider whose file could not be read.
    """
    _require_extra()
    from l3io.wp.config import MISSING, WpConfigError, WpConfigFile

    try:
        config = WpConfigFile(config_file)
        raw = {key: config.get(key) for key in keys}
    except WpConfigError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    return {key: None if v is MISSING else _constant(v) for key, v in raw.items()}


def _set_failure(exc: Exception) -> str:
    """Why a ``set`` failed, from the library error's structured fields."""
    from l3io.wp.config import (
        ConfigKeyError,
        ConfigValueError,
        ConfigWriteError,
        ReadBackError,
    )

    if isinstance(exc, ConfigValueError):
        return "unrenderable"
    if isinstance(exc, ConfigKeyError):
        return "not a PHP identifier"
    if isinstance(exc, ReadBackError):
        return _READ_BACK_REASON
    if isinstance(exc, ConfigWriteError):
        return _OWNER_REASON if _OWNER_MARKER in exc.reason else exc.reason
    return type(exc).__name__


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory_after_commit(directory: Path) -> None:
    """Make a rename durable; logged, not raised, since the file has changed.

    Raising here would report a replaced file as unchanged, as
    ``l3io-wordpress-config`` reasons for its own writes.
    """
    try:
        _fsync_directory(directory)
    except OSError as exc:
        _log.warning(
            "config_directory_not_synced",
            path=str(directory),
            reason=exc.strerror or type(exc).__name__,
        )


def refuse_unkeepable(
    config_file: Path, keep: Mapping[str, ConstantValue]
) -> dict[str, str | int | bool]:
    """``keep`` without ``None``s; a ``None`` (not defined) is refused.

    ``ConfigWriteFailedError`` names the key. Pure: the Dry Run's
    ``NoopConfigWriter.replace_file`` calls it too (AD-4; BL-E002-093).
    """
    kept: dict[str, str | int | bool] = {}
    for key, value in keep.items():
        if value is None:
            absent = "the target does not define it, so it cannot be kept"
            raise ConfigWriteFailedError(config_file, key, absent)
        kept[key] = value
    return kept


class WpConfigWriter:
    """``ConfigQuery`` and ``ConfigCommand`` over ``l3io-wordpress-config``."""

    __slots__ = ("_secrets",)

    def __init__(self, secrets: SecretRegistry) -> None:
        self._secrets = secrets

    def __repr__(self) -> str:
        return "WpConfigWriter()"

    def _register(self, values: Mapping[str, object], *, source: str) -> None:
        value = values.get(_PASSWORD_KEY)
        if isinstance(value, str):
            self._secrets.register(value, source=source)

    # ------------------------------------------------------------------
    # ConfigQuery
    # ------------------------------------------------------------------

    def read_constants(
        self, config_file: Path, keys: Sequence[str]
    ) -> Mapping[str, ConstantValue]:
        """The restore target's ``keys``; ``None`` for an undefined one."""
        label = f"wp-config:{config_file}"
        values = read_defined(config_file, keys, label=label)
        self._register(values, source=label)
        return values

    # ------------------------------------------------------------------
    # ConfigCommand
    # ------------------------------------------------------------------

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        """Set ``values`` in ``config_file``, one atomic verified write per key.

        **Not one transaction.** Each key, in the order given, is its own
        ``WpConfigFile.set``. When one fails, ``ConfigWriteFailedError`` names
        it and ``written`` lists the keys whose write had already landed; the
        file holds exactly those changes. A key already at its value is not
        rewritten and is absent from ``changed``, which the caller journals
        per key. The file must exist: it is never created here.
        """
        _require_extra()
        from l3io.wp.config import ConfigReadError, WpConfigError, WpConfigFile

        self._register(values, source="set_constants")
        started = time.monotonic_ns()
        keys = tuple(values)
        written: list[str] = []
        first = keys[0] if keys else WHOLE_FILE
        try:
            config = WpConfigFile(config_file)
        except ConfigReadError as exc:
            unread = _ABSENT_REASON if not config_file.exists() else exc.reason
            raise ConfigWriteFailedError(config_file, first, unread) from exc
        for key in keys:
            try:
                if config.set(key, values[key]):
                    written.append(key)
            except WpConfigError as exc:
                failure = _set_failure(exc)
                raise ConfigWriteFailedError(
                    config_file, key, failure, written=tuple(written)
                ) from exc
        _log.info(
            "config_written",
            path=str(config_file),
            method="set_constants",
            duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
            keys=list(keys),
            changed=list(written),
        )
        return ConfigWriteResult(changed=tuple(written), performed=True)

    def save_copy(self, config_file: Path, destination: Path) -> ConfigWriteResult:
        """Copy ``config_file`` to ``destination`` at mode ``0600``, fsynced.

        Written beside ``destination`` and moved onto it, so ``destination``
        is either absent or complete. A missing ``destination`` directory is
        created ``0700`` first, so a Dry Run (whose twin writes nothing)
        creates none either (AD-4).
        """
        started = time.monotonic_ns()
        directory = destination.parent
        try:
            private_directories(directory)
            fd, name = tempfile.mkstemp(
                dir=directory, prefix=f".{destination.name}.", suffix=_TMP_SUFFIX
            )
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ConfigWriteFailedError(destination, WHOLE_FILE, reason) from exc
        tmp = Path(name)
        committed = False
        try:
            with os.fdopen(fd, "wb") as handle, config_file.open("rb") as src:
                os.fchmod(handle.fileno(), 0o600)
                shutil.copyfileobj(src, handle)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(destination)
            committed = True
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ConfigWriteFailedError(destination, WHOLE_FILE, reason) from exc
        finally:
            if not committed:
                tmp.unlink(missing_ok=True)
        _sync_directory_after_commit(directory)
        _log.info(
            "config_written",
            path=str(destination),
            method="save_copy",
            duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
            keys=[],
        )
        return ConfigWriteResult(changed=(), performed=True)

    def replace_file(
        self,
        config_file: Path,
        content: str,
        *,
        keep: Mapping[str, ConstantValue],
    ) -> ConfigWriteResult:
        """Replace ``config_file`` with the text ``content``, ``keep`` applied first.

        ``content`` arrives in memory, so no intermediate copy of the Archive's
        config is ever written (BL-E003-057). ``keep`` is set into the text in
        memory, so the replacement's values for
        those keys are never written anywhere. A ``keep`` value of ``None`` (a
        constant the target does not define) cannot be kept and is refused.
        The write is ``l3io-wordpress-config``'s ``WpConfigFile.replace_content``
        (0.1.4, BL-E002-091): a temp file beside the config takes the
        original's owner and mode before any content — refusing rather than
        changing the owner — is fsynced, read back, and ``os.replace``d over
        ``config_file``; the directory is then fsynced. A text that defines a
        key twice is refused. ``config_file`` must exist. On any failure the
        original is byte-identical and no temp file is left behind.
        """
        _require_extra()
        from l3io.wp.config import (
            ConfigReadError,
            WpConfigError,
            WpConfigFile,
            WpConfigString,
        )

        self._register(keep, source="replace_file")
        started = time.monotonic_ns()
        kept = refuse_unkeepable(config_file, keep)
        try:
            target = WpConfigFile(config_file)
        except ConfigReadError as exc:
            unread = _ABSENT_REASON if not config_file.exists() else exc.reason
            raise ConfigWriteFailedError(config_file, WHOLE_FILE, unread) from exc
        try:
            working = WpConfigString(content)
        except ValueError as exc:
            unread = f"the replacement could not be read: {type(exc).__name__}"
            raise ConfigWriteFailedError(config_file, WHOLE_FILE, unread) from exc
        self._register(
            {_PASSWORD_KEY: working.get(_PASSWORD_KEY)}, source="replace_file"
        )
        changed: list[str] = []
        for key, value in kept.items():
            try:
                if working.set(key, value):
                    changed.append(key)
            except WpConfigError as exc:
                raise ConfigWriteFailedError(
                    config_file, key, _set_failure(exc)
                ) from exc
        try:
            target.replace_content(working.content)
        except WpConfigError as exc:
            raise ConfigWriteFailedError(
                config_file, WHOLE_FILE, _set_failure(exc)
            ) from exc
        _log.info(
            "config_written",
            path=str(config_file),
            method="replace_file",
            duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
            keys=list(keep),
            changed=changed,
        )
        return ConfigWriteResult(changed=tuple(changed), performed=True)
