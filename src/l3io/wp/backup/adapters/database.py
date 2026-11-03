"""ADR-0015 — the backup half of the one module that calls ``l3io-wp-database``.

Locates ``wp-config.php``, builds the Site Descriptor from it (AD-3), resolves
the WordPress credentials from it when no Credential Provider is given, checks
dump readiness and produces the Dump through database's ``dump``. Nothing else
in the package opens ``wp-config.php`` for reading (AD-3), and this package
invokes no MySQL client tool and writes no option file.

``l3io.wp.database`` is the optional ``database`` extra, imported **inside the
functions that use it**; its absence raises ``MissingExtraError`` at the first
call, before any write (AD-8). Every database exception reaching a public
boundary is re-raised as a package error with ``raise ... from`` (AD-14).
Credentials are passed straight from database's sources to database's
functions and are never stored, logged or put in an exception message.

This is the only module that constructs database's credential classes
(ADR-0015 amendment): :func:`wordpress_source` and :func:`admin_source` turn a
backup-owned ``CredentialSpec`` into a source. Every source they return
registers each password it resolves with the Run's ``SecretRegistry``
immediately after ``resolve()`` (ADR-0018), and an explicit spec's password is
registered when the source is built. Usernames are not secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.archive_format import ConfigLocation
from l3io.wp.backup.domain.credentials import (
    AwsSecretCredentialSpec,
    CredentialSpec,
    ExplicitCredentialSpec,
)
from l3io.wp.backup.domain.database import DatabaseLocation, DumpResult, Readiness
from l3io.wp.backup.domain.errors import (
    BackupError,
    CredentialsError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    DumpFailedError,
    InvalidSiteDescriptorError,
    MissingExtraError,
    UsageError,
)
from l3io.wp.backup.domain.site import SiteDescriptor

if TYPE_CHECKING:
    from l3io.wp.database import (
        Connection,
        CredentialSource,
        ResolvedCredentials,
        Verdict,
    )

__all__ = [
    "LocatedConfig",
    "WpDatabase",
    "admin_source",
    "blocked_dump_error",
    "locate_wp_config",
    "require_wp_config_credentials",
    "site_descriptor_from_archive_config",
    "site_descriptor_from_wp_config",
    "wordpress_source",
]

_log = structlog.get_logger(__name__)

_EXTRA = "database"
_CONFIG_NAME = "wp-config.php"
_SETTINGS_NAME = "wp-settings.php"

#: Finding codes that mean the credentials, not the server, were the problem.
_CREDENTIAL_CODES = frozenset({"authentication_failed", "credentials_unobtainable"})
_UNREACHABLE_CODE = "unreachable"
_MISSING_PRIVILEGE = "missing_privilege"


def _require_extra() -> None:
    """Raise ``MissingExtraError`` when the ``database`` extra is absent (AD-8)."""
    try:
        import l3io.wp.database  # noqa: F401 - imported for its presence only
    except ImportError as exc:
        raise MissingExtraError(extra=_EXTRA) from exc


# --------------------------------------------------------------------------
# Locating wp-config.php
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocatedConfig:
    """The ``wp-config.php`` a Run reads, and which search location it is."""

    path: Path
    location: ConfigLocation | None


def _site_root(site_root: Path) -> Path:
    try:
        resolved = site_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        msg = "site root does not exist"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),)) from exc
    if not resolved.is_dir():
        msg = "site root is not a directory"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),))
    return resolved


def locate_wp_config(site_root: Path, *, stated: Path | None) -> LocatedConfig:
    """Find the ``wp-config.php`` for ``site_root``, the way ``wp-load.php`` does.

    In order: ``stated`` is used as given (canonicalised, never re-derived);
    else ``<root>/wp-config.php``; else ``<root>/../wp-config.php`` when the
    parent holds no ``wp-settings.php``; else ``InvalidSiteDescriptorError``
    naming both paths searched.
    """
    root = _site_root(site_root)
    in_root = root / _CONFIG_NAME
    in_parent = root.parent / _CONFIG_NAME
    if stated is not None:
        try:
            path = stated.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            msg = "the stated wp-config.php does not exist"
            raise InvalidSiteDescriptorError(msg, paths=(str(stated),)) from exc
        if not path.is_file():
            msg = "the stated wp-config.php is not a regular file"
            raise InvalidSiteDescriptorError(msg, paths=(str(stated),))
        location = _location_of(path, in_root, in_parent)
        if location is None:
            _log.warning("wp_config_outside_search_path", path=str(path))
    elif in_root.is_file():
        path, location = in_root, ConfigLocation.SITE_ROOT
    elif in_parent.is_file() and not (root.parent / _SETTINGS_NAME).exists():
        path, location = in_parent, ConfigLocation.PARENT
    else:
        msg = "no usable wp-config.php"
        raise InvalidSiteDescriptorError(msg, paths=(str(in_root), str(in_parent)))
    _log.info(
        "wp_config_located",
        path=str(path),
        location=None if location is None else location.value,
    )
    return LocatedConfig(path=path, location=location)


def _location_of(path: Path, in_root: Path, in_parent: Path) -> ConfigLocation | None:
    for candidate, location in (
        (in_root, ConfigLocation.SITE_ROOT),
        (in_parent, ConfigLocation.PARENT),
    ):
        if candidate.is_file() and candidate.resolve() == path:
            return location
    return None


# --------------------------------------------------------------------------
# The Site Descriptor
# --------------------------------------------------------------------------


def site_descriptor_from_wp_config(
    site_root: Path, *, config_file: Path
) -> tuple[SiteDescriptor, CredentialSource]:
    """The descriptor for a live Site, and its ``wp-config.php`` credential source."""
    root = _site_root(site_root)
    if not config_file.is_file():
        msg = "wp-config.php is not a readable regular file"
        raise InvalidSiteDescriptorError(msg, paths=(str(config_file),))
    return _parse(PurePosixPath(root), config_file)


def site_descriptor_from_archive_config(
    site_root: Path, *, config_file: Path | None
) -> tuple[SiteDescriptor, CredentialSource]:
    """The descriptor for a restore target with no ``wp-config.php`` (AD-3).

    ``config_file`` is the Archive's config member, already extracted into the
    Run workspace; ``site_root`` is the target's canonical Site Root.
    """
    if config_file is None or not config_file.is_file():
        msg = "archive has no wp-config.php"
        raise InvalidSiteDescriptorError(
            msg, paths=() if config_file is None else (str(config_file),)
        )
    if not site_root.is_absolute():
        msg = "site root must be absolute"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),))
    return _parse(PurePosixPath(site_root), config_file)


def _parse(
    site_root: PurePosixPath, config_file: Path
) -> tuple[SiteDescriptor, CredentialSource]:
    """The one parsing path both descriptor constructors share.

    Credential-free: connection, table prefix and URLs only. The descriptor
    carries no credential, so a config whose credentials come from elsewhere
    (an explicit spec, the environment, AWS) still yields one; resolving the
    pair is ``require_wp_config_credentials``'s job.
    """
    _require_extra()
    from l3io.wp import database as wpdb

    label = f"wp-config:{config_file}"
    try:
        config = wpdb.WpConfigFile(config_file)
        label = config.source
        connection = config.connection()
        table_prefix = config.table_prefix
    except wpdb.MissingDependencyError as exc:
        raise MissingExtraError(extra=_EXTRA) from exc
    except wpdb.ConfigUnreadableError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    except wpdb.MissingConfigValueError as exc:
        msg = f"{exc.key} is not defined"
        raise InvalidSiteDescriptorError(msg, paths=(str(config_file),)) from exc
    except (wpdb.InvalidArgumentsError, wpdb.InvalidIdentifierError) as exc:
        raise InvalidSiteDescriptorError(str(exc), paths=(str(config_file),)) from exc
    site_url, home_url = _url_constants(config_file, label)
    descriptor = SiteDescriptor(
        site_root=site_root,
        db_host=connection.host,
        db_port=connection.port,
        db_name=connection.database,
        table_prefix=table_prefix,
        site_url=site_url,
        home_url=home_url,
        credential_source=label,
    )
    return descriptor, config


def require_wp_config_credentials(source: CredentialSource) -> None:
    """Resolve ``source`` once, so a half-defined pair fails before anything runs.

    Called only where ``wp-config.php`` is the Credential Provider (no other
    provider given, FR-1). The result is discarded; ``dump`` resolves again
    from the source. Raises ``CredentialsError`` naming the source label and,
    for a half pair, the missing key — never a value.
    """
    _require_extra()
    from l3io.wp import database as wpdb

    label = source.source
    try:
        source.resolve()
    except wpdb.MissingDependencyError as exc:
        raise MissingExtraError(extra=_EXTRA) from exc
    except wpdb.ConfigUnreadableError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    except wpdb.CredentialsUnobtainableError as exc:
        cause = exc.__cause__
        key = cause.key if isinstance(cause, wpdb.MissingConfigValueError) else None
        raise CredentialsError(label, missing_key=key) from exc
    except wpdb.CredentialsAbsentError as exc:
        raise CredentialsError(
            label, reason="defines neither DB_USER nor DB_PASSWORD"
        ) from exc


def _url_constants(config_file: Path, label: str) -> tuple[str | None, str | None]:
    """``WP_SITEURL`` and ``WP_HOME``, or ``None`` where not defined.

    Database's ``WpConfigFile`` exposes neither, so they are read through
    ``l3io-wordpress-config`` — the reader database itself delegates to.
    """
    from l3io.wp.config import MISSING, WpConfigError
    from l3io.wp.config import WpConfigFile as ConfigFile

    try:
        config = ConfigFile(filename=config_file)
        values = (config.get("WP_SITEURL"), config.get("WP_HOME"))
    except WpConfigError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    site_url, home_url = (None if v is MISSING else str(v) for v in values)
    return site_url, home_url


# --------------------------------------------------------------------------
# Credential sources — the only construction of database's credential classes
# --------------------------------------------------------------------------

_AWS_NOT_BOUND = (
    "an AWS Secrets Manager credential needs Epic 5's provider, "
    "which this build does not bind"
)


class _RegisteringSource:
    """A ``CredentialSource`` that registers each password it resolves.

    Registration happens immediately after the wrapped ``resolve()``, so a
    password is known to the Run's registry before anything can emit it. A
    failure that is not one of database's own credential errors is re-raised
    as ``CredentialsError`` naming the label and the failure class; its text
    is untrusted, so it is not chained.
    """

    __slots__ = ("_inner", "_secrets")

    def __init__(self, inner: CredentialSource, secrets: SecretRegistry) -> None:
        self._inner = inner
        self._secrets = secrets

    def __repr__(self) -> str:
        return f"RegisteringSource({self.source!r})"

    @property
    def source(self) -> str:
        return self._inner.source

    @property
    def secrets(self) -> SecretRegistry:
        return self._secrets

    def resolve(self) -> ResolvedCredentials:
        from l3io.wp import database as wpdb

        label = self.source
        failure: str | None = None
        try:
            resolved = self._inner.resolve()
        except (wpdb.DatabaseError, BackupError):
            raise
        except Exception as exc:
            failure = type(exc).__name__
        if failure is not None:
            # Raised outside the handler, so not even __context__ holds the
            # untrusted exception.
            raise CredentialsError(label, reason=failure)
        self._secrets.register(resolved.password, source=label)
        return resolved


def _registering(source: CredentialSource, secrets: SecretRegistry) -> CredentialSource:
    """``source`` registering into ``secrets``; re-wrapped if bound to another."""
    if isinstance(source, _RegisteringSource) and source.secrets is secrets:
        return source
    return _RegisteringSource(source, secrets)


def _explicit(
    spec: ExplicitCredentialSpec, secrets: SecretRegistry
) -> CredentialSource:
    from l3io.wp import database as wpdb

    secrets.register(spec.password, source="explicit")
    return wpdb.Explicit(spec.username, spec.password)


def wordpress_source(
    spec: CredentialSpec | None,
    *,
    wp_config: CredentialSource,
    environ: Mapping[str, str],  # noqa: ARG001 - Epic 5 binds the environment step
    secrets: SecretRegistry,
) -> CredentialSource:
    """The WordPress account's source, in ``WORDPRESS_CREDENTIAL_PRECEDENCE`` order.

    ``None`` is ``wp-config.php`` alone. An explicit spec comes first, then
    ``wp-config.php``; the environment step is skipped until Epic 5 binds its
    provider. An AWS spec is ``UsageError`` until then.
    """
    _require_extra()
    from l3io.wp import database as wpdb

    if spec is None:
        return _registering(wp_config, secrets)
    if isinstance(spec, AwsSecretCredentialSpec):
        raise UsageError(_AWS_NOT_BOUND)
    chain = wpdb.CredentialChain(_explicit(spec, secrets), wp_config)
    return _registering(chain, secrets)


def admin_source(
    spec: CredentialSpec | None,
    *,
    environ: Mapping[str, str],  # noqa: ARG001 - Epic 5 binds the environment step
    secrets: SecretRegistry,
) -> CredentialSource | None:
    """The administrator's source, or ``None``: never ``wp-config.php``."""
    if spec is None:
        return None
    _require_extra()
    if isinstance(spec, AwsSecretCredentialSpec):
        raise UsageError(_AWS_NOT_BOUND)
    return _registering(_explicit(spec, secrets), secrets)


# --------------------------------------------------------------------------
# The database ports, backup half
# --------------------------------------------------------------------------


def blocked_dump_error(
    location: DatabaseLocation, blockers: tuple[str, ...], *, credential_source: str
) -> BackupError | None:
    """What a dump blocked by ``blockers`` fails with when the cause is the
    server (``DatabaseUnreachableError``) or the credentials
    (``CredentialsError``); ``None`` for any other finding."""
    if _UNREACHABLE_CODE in blockers:
        return DatabaseUnreachableError(location.host, location.port)
    if _CREDENTIAL_CODES.intersection(blockers):
        return CredentialsError(credential_source, reason=", ".join(blockers))
    return None


def _codes(verdict: Verdict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    blockers = tuple(str(f.code) for f in verdict.blockers)
    warnings = tuple(str(f.code) for f in verdict.warnings)
    return blockers, warnings


class WpDatabase:
    """``DatabaseQuery`` and ``DatabaseCommand``, backup halves.

    The WordPress ``CredentialSource`` and the Run's ``SecretRegistry`` are
    bound at construction; no method takes a credential. A source not built
    by :func:`wordpress_source` is wrapped so that what database resolves
    from it is registered all the same.
    """

    __slots__ = ("_credentials",)

    def __init__(
        self, credentials: CredentialSource, *, secrets: SecretRegistry
    ) -> None:
        self._credentials = _registering(credentials, secrets)

    def __repr__(self) -> str:
        return f"WpDatabase(credentials={self._credentials.source!r})"

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        """Database's ``check_dump``, its verdict copied as finding codes."""
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        try:
            verdict = wpdb.check_dump(connection, self._credentials)
        except wpdb.DatabaseUnreachableError as exc:
            raise DatabaseUnreachableError(location.host, location.port) from exc
        except wpdb.AuthenticationFailedError as exc:
            raise CredentialsError(self._credentials.source) from exc
        except wpdb.AccessDeniedError as exc:
            # An authenticated account lacking a privilege mid-session.
            raise DatabaseRefusedError(findings=(_MISSING_PRIVILEGE,)) from exc
        except wpdb.MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except wpdb.DatabaseError as exc:
            raise DumpFailedError(type(exc).__name__) from exc
        blockers, warnings = _codes(verdict)
        _log.info(
            "dump_readiness",
            db_host=location.host,
            db_port=location.port,
            db_name=location.name,
            credential_source=self._credentials.source,
            blockers=blockers,
            warnings=warnings,
        )
        return Readiness(ready=verdict.ok, blockers=blockers, warnings=warnings)

    def dump_to(self, location: DatabaseLocation, destination: Path) -> DumpResult:
        """Database's ``dump``, written plain and atomically at mode ``0600``.

        No key argument is passed: the Dump is encrypted, if at all, inside the
        payload (ADR-0024).
        """
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        source = self._credentials.source
        try:
            result = wpdb.dump(connection, self._credentials, destination)
        except wpdb.RefusedError as exc:
            raise self._refusal(location, exc.verdict) from exc
        except wpdb.TransferError as exc:
            # Structured fields only, never the dependency's text (AD-7).
            code = exc.exit_code
            reason = "the dump stopped" if code is None else f"the dump exited {code}"
            raise DumpFailedError(reason) from exc
        except wpdb.DatabaseUnreachableError as exc:
            raise DatabaseUnreachableError(location.host, location.port) from exc
        except (
            wpdb.AuthenticationFailedError,
            wpdb.AccessDeniedError,
            wpdb.CredentialsUnobtainableError,
            wpdb.CredentialsAbsentError,
        ) as exc:
            raise CredentialsError(source) from exc
        except wpdb.MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except wpdb.DatabaseError as exc:
            raise DumpFailedError(type(exc).__name__) from exc
        except OSError as exc:
            raise DumpFailedError(exc.strerror or type(exc).__name__) from exc
        return DumpResult(bytes=result.bytes, duration_ms=result.duration_ms)

    def _refusal(self, location: DatabaseLocation, verdict: Verdict) -> Exception:
        blockers, _ = _codes(verdict)
        error = blocked_dump_error(
            location, blockers, credential_source=self._credentials.source
        )
        return DatabaseRefusedError(findings=blockers) if error is None else error

    @staticmethod
    def _connection(location: DatabaseLocation) -> Connection:
        """The one place a database ``Connection`` is built from a location."""
        from l3io.wp import database as wpdb

        try:
            return wpdb.Connection(
                host=location.host, database=location.name, port=location.port
            )
        except (wpdb.InvalidArgumentsError, wpdb.InvalidIdentifierError) as exc:
            raise InvalidSiteDescriptorError(str(exc)) from exc
