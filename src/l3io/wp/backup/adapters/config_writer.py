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

**Values from the environment (owner decision 47).** :func:`read_settings`
and ``read_constants`` read a key through ``get_value``/``get_variable_value``
(0.1.5): a literal is used as written; an ``EnvLookup``
(``getenv_docker('NAME', <literal>)`` or ``getenv('NAME')``, as the official
``wordpress`` image writes them) is resolved by the library's
``EnvLookup.resolve`` against the Run's ``environ`` mapping, with
:func:`read_secret_file` for a ``NAME_FILE`` Docker secret; anything else is
an ``Expression`` and is refused (``UnsupportedConfigValueError``) without
quoting it. Nothing here parses PHP or reimplements a fallback, and no adapter
reads ``os.environ`` (BL-E005-007): the composition roots hand ``environ`` in.
:func:`refuse_environment_lookups` is the restore's early refusal of an
environment-driven target (ADR-0027, Proposed).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.workspace import private_directories
from l3io.wp.backup.domain.config import ConfigWriteResult, ConstantValue
from l3io.wp.backup.domain.errors import (
    BackupError,
    ConfigWriteFailedError,
    CredentialsError,
    EnvironmentDrivenTargetError,
    InvalidSiteDescriptorError,
    MissingExtraError,
    UnsupportedConfigValueError,
)
from l3io.wp.backup.ports.config import WHOLE_FILE

if TYPE_CHECKING:
    from l3io.wp.config import EnvLookup

__all__ = [
    "TABLE_PREFIX",
    "WpConfigWriter",
    "read_defined",
    "read_secret_file",
    "read_settings",
    "refuse_environment_lookups",
    "refuse_unkeepable",
]

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
#: ``$table_prefix``, named without its ``$`` as the library names a variable.
TABLE_PREFIX: Final = "table_prefix"
#: The keys whose lookup failure is a credentials failure, not a site one.
_CREDENTIAL_KEYS: Final = frozenset({"DB_USER", _PASSWORD_KEY})
#: The longest ``NAME_FILE`` this reads, in characters: a Docker secret is one
#: short value, and a larger file is refused rather than read whole.
SECRET_FILE_LIMIT: Final = 64 * 1024
_NO_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType({})


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


def _shown(key: str) -> str:
    """``key`` as an operator reads it: ``$table_prefix`` for the variable."""
    return f"${key}" if key == TABLE_PREFIX else key


def read_secret_file(path: str) -> str:
    """The text of the file a ``NAME_FILE`` variable names (a Docker secret).

    ``EnvLookup.resolve``'s ``read_file``: it strips the trailing newline
    itself. A file over :data:`SECRET_FILE_LIMIT` characters is a
    ``ValueError`` carrying no text; the library names the failure by class.
    """
    with Path(path).open(encoding="utf-8") as handle:
        text = handle.read(SECRET_FILE_LIMIT + 1)
    if len(text) > SECRET_FILE_LIMIT:
        raise ValueError
    return text


def _lookup_failed(
    key: str, variable: str, reason: str, *, config_file: Path, label: str
) -> BackupError:
    """A lookup that gave no value: credentials for ``DB_USER``/``DB_PASSWORD``."""
    why = f"{_shown(key)} reads the environment variable {variable}: {reason}"
    if key in _CREDENTIAL_KEYS:
        return CredentialsError(label, reason=why)
    return InvalidSiteDescriptorError(why, paths=(str(config_file),))


def _resolve(
    key: str,
    lookup: EnvLookup,
    *,
    environ: Mapping[str, str],
    config_file: Path,
    label: str,
) -> str:
    """``lookup``'s value as PHP would define it, as text; never logged.

    The library's ``ConfigLookupError`` names the variable only; it is turned
    into this package's error outside its handler, so neither it nor what the
    file reader raised (a ``UnicodeDecodeError`` holds the file's bytes) is
    chained. A lookup PHP resolves to ``false`` (a bare ``getenv('NAME')``
    of an unset variable) defines no usable setting and is refused as unset.
    """
    from l3io.wp.config import ConfigLookupError

    failure: str | None = None
    value: object = None
    try:
        value = lookup.resolve(environ, read_file=read_secret_file)
    except ConfigLookupError as exc:
        failure = exc.reason
    if failure is not None:
        raise _lookup_failed(
            key, lookup.name, failure, config_file=config_file, label=label
        )
    if value is False:
        raise _lookup_failed(
            key, lookup.name, "it is not set", config_file=config_file, label=label
        )
    _log.info(
        "config_value_from_environment",
        path=str(config_file),
        key=_shown(key),
        variable=lookup.name,
        function=lookup.function,
    )
    return value if isinstance(value, str) else str(value)


def _read_values(
    config_file: Path,
    keys: Sequence[str],
    *,
    environ: Mapping[str, str],
    label: str,
    secrets: SecretRegistry | None,
) -> dict[str, ConstantValue]:
    """``keys``' values, an ``EnvLookup`` resolved; ``None`` for an undefined one.

    :data:`TABLE_PREFIX` is read as the variable. A literal keeps its PHP
    type; a resolved lookup is text. A resolved ``DB_PASSWORD`` is registered
    with ``secrets`` before anything else happens to it.
    """
    _require_extra()
    from l3io.wp.config import (
        MISSING,
        EnvLookup,
        Expression,
        WpConfigError,
        WpConfigFile,
    )

    try:
        config = WpConfigFile(config_file)
        raw = {
            key: config.get_variable_value(key)
            if key == TABLE_PREFIX
            else config.get_value(key)
            for key in keys
        }
    except WpConfigError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    for key, value in raw.items():
        if isinstance(value, Expression):
            raise UnsupportedConfigValueError(_shown(key), path=config_file)
    values: dict[str, ConstantValue] = {}
    for key, value in raw.items():
        if value is MISSING:
            values[key] = None
        elif isinstance(value, EnvLookup):
            text = _resolve(
                key, value, environ=environ, config_file=config_file, label=label
            )
            if key == _PASSWORD_KEY and secrets is not None:
                secrets.register(text, source=f"{label}:{value.name}")
            values[key] = text
        else:
            values[key] = _constant(value)
    return values


def read_settings(
    config_file: Path,
    keys: Sequence[str],
    *,
    environ: Mapping[str, str],
    label: str,
    secrets: SecretRegistry | None = None,
) -> dict[str, str | None]:
    """``keys``' values as text, each ``EnvLookup`` resolved against ``environ``.

    The reader of the Site's ``DB_*`` and ``$table_prefix`` (owner decision
    47); :data:`TABLE_PREFIX` names the variable. ``None`` is an undefined
    key. A literal that is not a string becomes its text, as database's
    reader did. Refused: an ``Expression`` (``UnsupportedConfigValueError``,
    ``REFUSED``); a lookup that cannot be resolved -- a ``NAME_FILE`` that
    cannot be read, a bare ``getenv`` of an unset variable -- is
    ``CredentialsError`` for ``DB_USER``/``DB_PASSWORD`` and
    ``InvalidSiteDescriptorError`` otherwise, naming the constant and the
    variable, never a value. An unreadable file is ``CredentialsError(label)``.
    """
    values = _read_values(
        config_file, keys, environ=environ, label=label, secrets=secrets
    )
    return {key: None if v is None else str(v) for key, v in values.items()}


def refuse_environment_lookups(
    config_file: Path, keys: Sequence[str], *, operation: str, label: str
) -> None:
    """Refuse when any of ``keys`` is read from the environment (ADR-0027).

    Nothing is resolved, so no value is read. ``EnvironmentDrivenTargetError``
    names every such key and its variable, and ``operation``; an
    ``Expression`` among ``keys`` is ``UnsupportedConfigValueError``.
    """
    _require_extra()
    from l3io.wp.config import EnvLookup, Expression, WpConfigError, WpConfigFile

    try:
        config = WpConfigFile(config_file)
        raw = {key: config.get_value(key) for key in keys}
    except WpConfigError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    for key, value in raw.items():
        if isinstance(value, Expression):
            raise UnsupportedConfigValueError(_shown(key), path=config_file)
    lookups = [(k, v.name) for k, v in raw.items() if isinstance(v, EnvLookup)]
    if lookups:
        raise EnvironmentDrivenTargetError(
            keys=tuple(_shown(k) for k, _ in lookups),
            variables=tuple(name for _, name in lookups),
            path=config_file,
            operation=operation,
        )


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
    """``ConfigQuery`` and ``ConfigCommand`` over ``l3io-wordpress-config``.

    ``environ`` is the Run's environment mapping, which ``read_constants``
    resolves an ``EnvLookup`` against (owner decision 47); empty by default,
    so a lookup then takes its default, as PHP would with nothing set.
    """

    __slots__ = ("_environ", "_secrets")

    def __init__(
        self, secrets: SecretRegistry, *, environ: Mapping[str, str] = _NO_ENVIRONMENT
    ) -> None:
        self._secrets = secrets
        self._environ = environ

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
        """The restore target's ``keys``; ``None`` for an undefined one.

        A literal keeps its PHP type, an ``EnvLookup`` is resolved against the
        writer's ``environ`` and an ``Expression`` is refused
        (``UnsupportedConfigValueError``): its source text is never handed on
        as if it were a value (BL-E008-032).
        """
        label = f"wp-config:{config_file}"
        values = _read_values(
            config_file,
            keys,
            environ=self._environ,
            label=label,
            secrets=self._secrets,
        )
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
