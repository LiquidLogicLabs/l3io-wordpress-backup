"""ADR-0015 — the one module that calls ``l3io-wp-database``, both halves.

Locates ``wp-config.php``, builds the Site Descriptor from it (AD-3), resolves
the WordPress credentials from it when no Credential Provider is given, checks
dump readiness and produces the Dump through database's ``dump``; on restore
it surveys, ensures, loads into, revokes on and drops a database through
database's functions of those names (E002-S02-009). This package issues no
SQL and quotes no identifier: AD-28's grant escaping is database's. Nothing
else in the package builds a Site Descriptor from ``wp-config.php`` (AD-3);
``WP_SITEURL``/``WP_HOME`` and a restore target's constants are read through
``adapters/config_writer``. This package invokes no MySQL client tool and
writes no option file.

``l3io.wp.database`` is the optional ``database`` extra, imported **inside the
functions that use it**; its absence raises ``MissingExtraError`` at the first
call, before any write (AD-8). Every database exception reaching a public
boundary is re-raised as a package error with ``raise ... from`` (AD-14).
Credentials are passed straight from database's sources to database's
functions and are never stored, logged or put in an exception message.

This module and ``adapters/credential_providers.py`` are the only ones that
construct database's credential classes (ADR-0015 amendment):
:func:`wordpress_source` and :func:`admin_source` turn a backup-owned
``CredentialSpec`` into a source through that module's builders (FR-14).
Every source they return registers each password it resolves with the Run's
``SecretRegistry`` immediately after ``resolve()`` (ADR-0018), and an explicit
spec's password is registered when the source is built. Usernames are not
secrets.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Final

import structlog

from l3io.wp.backup.adapters.config_writer import read_defined
from l3io.wp.backup.adapters.credential_providers import (
    ADMIN_PASSWORD_VAR,
    ADMIN_USER_VAR,
    WORDPRESS_PASSWORD_VAR,
    WORDPRESS_USER_VAR,
    admin_credentials,
    unset_partner,
    wordpress_credentials,
)
from l3io.wp.backup.adapters.redaction import MIN_SECRET_LENGTH, SecretRegistry
from l3io.wp.backup.domain.archive_format import ConfigLocation
from l3io.wp.backup.domain.credentials import CredentialSpec
from l3io.wp.backup.domain.database import (
    DatabaseLocation,
    DatabaseTargetState,
    DumpResult,
    EnsureOutcome,
    LoadResult,
    Readiness,
)
from l3io.wp.backup.domain.errors import (
    AdminCredentialsRequiredError,
    BackupError,
    CredentialsError,
    CredentialTooShortError,
    DatabaseRefusedError,
    DatabaseUnreachableError,
    DumpFailedError,
    InvalidSiteDescriptorError,
    LoadFailedError,
    MissingExtraError,
    ProvisioningFailedError,
    RefusedError,
    UnreadableSiteRootError,
    UsageError,
)
from l3io.wp.backup.domain.site import SiteDescriptor

if TYPE_CHECKING:
    from l3io.wp.database import (
        Connection,
        CredentialSource,
        ResolvedCredentials,
        TlsSettings,
        Verdict,
    )

__all__ = [
    "ADMIN_FLAGS",
    "ADMIN_SOURCE",
    "ADMIN_TLS_MODES",
    "INVALID_DATABASE_NAME",
    "INVALID_DB_HOST",
    "TLS_SETTINGS_UNUSABLE",
    "AdminTls",
    "DatabaseAuthPluginRefusedError",
    "DatabaseTlsHandshakeError",
    "DatabaseTlsVerificationError",
    "LocatedConfig",
    "WpDatabase",
    "admin_source",
    "blocked_dump_error",
    "check_admin_tls",
    "database_location_from_db_host",
    "database_name_is_valid",
    "locate_wp_config",
    "password_at_call_time",
    "require_wp_config_credentials",
    "site_descriptor_from_archive_config",
    "site_descriptor_from_wp_config",
    "wordpress_source",
]

_log = structlog.get_logger(__name__)

_EXTRA = "database"
#: Database's extras that this package also offers under the same name. Every
#: other extra database names (``wpconfig``, ``encryption``) is installed by
#: this package's ``database`` extra, so the install hint names that.
_SHARED_EXTRAS: Final = frozenset({"aws"})
_CONFIG_NAME = "wp-config.php"
_SETTINGS_NAME = "wp-settings.php"

#: Finding codes that mean the credentials, not the server, were the problem.
_CREDENTIAL_CODES = frozenset({"authentication_failed", "credentials_unobtainable"})
_UNREACHABLE_CODE = "unreachable"
_MISSING_PRIVILEGE = "missing_privilege"

#: What binds the administrator, named by ``AdminCredentialsRequiredError``:
#: the flags, the AWS secret flag and the two environment variables.
ADMIN_FLAGS: Final = (
    "--admin-username",
    "--admin-password",
    "--admin-credentials-aws-secret-id",
    ADMIN_USER_VAR,
    ADMIN_PASSWORD_VAR,
)
#: The ``source`` label of that error: the half that was not given.
ADMIN_SOURCE: Final = "administrator"
#: The flag that supplies each account's AWS region, named when none is known.
_DB_REGION_FLAG: Final = "--db-credentials-aws-region"
_ADMIN_REGION_FLAG: Final = "--admin-credentials-aws-region"
#: Every restore operation names the WordPress account at any host.
_ANY_HOST: Final = "%"
#: The Dump is untrusted Archive content. Without ``--binary-mode`` the mysql
#: client executes client commands in it (a backslash-bang line runs a shell
#: command as the operator), so every load passes it (RT-S02-001).
_LOAD_OPTIONS: Final = ("--binary-mode",)
_NS_PER_MS: Final = 1_000_000


# --------------------------------------------------------------------------
# TLS for administrator connections (BL-E002-114)
# --------------------------------------------------------------------------

#: ``--admin-tls`` values, weakest first; each is a database ``TlsMode`` value
#: with ``-`` for ``_``. The policy may be raised to one of them, never lowered.
ADMIN_TLS_MODES: Final = ("required", "verify-ca", "verify-identity")
_TLS_ORDER: Final = ("off", "required", "verify_ca", "verify_identity")
#: ADV-S02-11: the ``UsageError`` texts that replace database's messages.
TLS_SETTINGS_UNUSABLE: Final = (
    "the TLS settings for the database connection could not be built from "
    "--admin-tls and --admin-tls-ca; nothing was changed"
)
INVALID_DB_HOST: Final = (
    "the database host is not HOST or HOST:PORT with a port from 1 to 65535; "
    "nothing was changed"
)
UNUSABLE_CONNECTION: Final = (
    "the database connection settings (host, port, name or TLS) are not usable"
)
INVALID_DATABASE_NAME: Final = (
    "the database name is not a valid database name; nothing was changed"
)


@dataclass(frozen=True, slots=True)
class AdminTls:
    """The restore's TLS override for its database connections.

    ``mode`` is an :data:`ADMIN_TLS_MODES` value or ``None`` (the policy for
    the host as it is); ``ca_file`` the CA bundle, ``None`` meaning the
    system store. Declared here so no ``l3io.wp.database`` type reaches
    ``cli/`` (ADR-0015 amendment).
    """

    mode: str | None
    ca_file: Path | None


class DatabaseTlsHandshakeError(DatabaseUnreachableError):
    """TLS was required and the server did not negotiate it. ``UNREACHABLE``."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(host, port)
        self.args = (
            f"database server {host}:{port} did not complete the required TLS "
            "handshake; nothing was changed",
        )


#: What of a server-sent plugin name may be shown: the server is untrusted.
_PLUGIN_NAME: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class DatabaseAuthPluginRefusedError(RefusedError):
    """The server asked for an authentication plugin that would expose the
    password (cleartext, old-password or dialog). ``REFUSED``.

    RT-S02-003: database (>= 0.3.2) refuses such a plugin before sending any
    password. The plugin name comes from the server, so only a plain name is
    shown; anything else is reported as ``unnamed``.
    """

    def __init__(self, host: str, port: int, plugin: str) -> None:
        self.host = host
        self.port = port
        self.plugin = plugin if _PLUGIN_NAME.fullmatch(plugin) else "unnamed"
        super().__init__(
            f"database server {host}:{port} asked for the {self.plugin} "
            "authentication plugin, which would expose the password; refused "
            "before any password was sent, and nothing was changed"
        )


class DatabaseTlsVerificationError(RefusedError):
    """The server's TLS certificate did not verify. ``REFUSED``."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"the TLS certificate of database server {host}:{port} did not verify "
            "(pass --admin-tls-ca FILE for a private CA); nothing was changed"
        )


def _tls_settings(admin_tls: AdminTls, host: str) -> TlsSettings:
    """``tls_required_unless_local(host)``, raised to ``admin_tls``; never lowered."""
    _require_extra()
    from l3io.wp import database as wpdb

    policy = wpdb.tls_required_unless_local(host, ca_file=admin_tls.ca_file)
    mode = policy.mode
    if admin_tls.mode is not None:
        if admin_tls.mode not in ADMIN_TLS_MODES:
            msg = f"--admin-tls {admin_tls.mode!r}: choose one of {ADMIN_TLS_MODES}"
            raise UsageError(msg, arguments=("--admin-tls",))
        wanted = wpdb.TlsMode(admin_tls.mode.replace("-", "_"))
        if _TLS_ORDER.index(wanted.value) < _TLS_ORDER.index(policy.mode.value):
            msg = (
                f"--admin-tls {admin_tls.mode} is below the policy for {host!r} "
                f"({policy.mode.value.replace('_', '-')}); it may only raise it"
            )
            raise UsageError(msg, arguments=("--admin-tls",))
        mode = wanted
    if mode is wpdb.TlsMode.OFF:
        if admin_tls.ca_file is not None:
            msg = f"--admin-tls-ca needs TLS, which is off for the local host {host!r}"
            raise UsageError(msg, arguments=("--admin-tls-ca",))
        return wpdb.TlsSettings()
    try:
        return wpdb.TlsSettings(mode=mode, ca_file=admin_tls.ca_file)
    except wpdb.InvalidArgumentsError as exc:
        # ADV-S02-11: a fixed text; database's message is never forwarded.
        raise UsageError(
            TLS_SETTINGS_UNUSABLE, arguments=("--admin-tls", "--admin-tls-ca")
        ) from exc


def check_admin_tls(admin_tls: AdminTls | None, host: str) -> None:
    """Raise ``UsageError`` now for an override no connection could use."""
    if admin_tls is not None:
        _tls_settings(admin_tls, host)


def _tls_error(exc: BaseException, location: DatabaseLocation) -> BackupError:
    """database's connection-security errors as the package's; never its text.

    TLS handshake and verification, and a refused authentication plugin.
    """
    from l3io.wp import database as wpdb

    if isinstance(exc, wpdb.AuthPluginRefusedError):
        return DatabaseAuthPluginRefusedError(
            location.host, location.port, str(exc.plugin)
        )
    if isinstance(exc, wpdb.TlsVerificationError):
        return DatabaseTlsVerificationError(location.host, location.port)
    return DatabaseTlsHandshakeError(location.host, location.port)


def _backup_extra(extra: str) -> str:
    """This package's extra for database's ``extra`` (review F-2)."""
    return extra if extra in _SHARED_EXTRAS else _EXTRA


def database_name_is_valid(name: str) -> bool:
    """Whether database's ``validate_database_name`` accepts ``name``.

    The one door for that check (ADR-0015): ``adapters/staging.py`` asks here
    instead of importing ``l3io.wp.database``. ``MissingExtraError`` without
    the ``database`` extra (AD-8).
    """
    _require_extra()
    from l3io.wp import database as wpdb

    try:
        wpdb.validate_database_name(name)
    except wpdb.InvalidIdentifierError:
        return False
    return True


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
        raise InvalidSiteDescriptorError(
            msg, paths=(str(Path(site_root).absolute()),)
        ) from exc
    if not resolved.is_dir():
        msg = "site root is not a directory"
        raise InvalidSiteDescriptorError(msg, paths=(str(site_root),))
    return resolved


def locate_wp_config(site_root: Path, *, stated: Path | None) -> LocatedConfig:
    """Find the ``wp-config.php`` for ``site_root``, the way ``wp-load.php`` does.

    In order: ``stated`` is used as given (canonicalised, never re-derived);
    else ``<root>/wp-config.php``; else ``<root>/../wp-config.php`` when the
    parent holds no ``wp-settings.php``; else ``InvalidSiteDescriptorError``
    naming both paths searched. A Site Root that exists and cannot be
    searched (``EACCES``) is :class:`UnreadableSiteRootError`, a refusal
    (BL-E003-034).
    """
    root = _site_root(site_root)
    try:
        return _locate(root, stated)
    except OSError as exc:
        raise UnreadableSiteRootError(root, type(exc).__name__) from exc


def _locate(root: Path, stated: Path | None) -> LocatedConfig:
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
        raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
    except wpdb.ConfigUnreadableError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    except wpdb.MissingConfigValueError as exc:
        msg = f"{exc.key} is not defined"
        raise InvalidSiteDescriptorError(msg, paths=(str(config_file),)) from exc
    except (wpdb.InvalidArgumentsError, wpdb.InvalidIdentifierError) as exc:
        raise InvalidSiteDescriptorError(
            _unusable_connection(exc), paths=(str(config_file),)
        ) from exc
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


def _unusable_connection(exc: Exception) -> str:
    """Why database refused the connection settings, from its fields only.

    An identifier refusal names the kind and database's fixed reason, never
    the value; any other is a fixed text (AD-7: no dependency text forwarded).
    """
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return f"the {kind} is not a valid identifier: {getattr(exc, 'reason', '')}"
    return UNUSABLE_CONNECTION


def _failure(
    failed: type[ProvisioningFailedError] | type[LoadFailedError],
    reason: str,
    location: DatabaseLocation,
) -> ProvisioningFailedError | LoadFailedError:
    """``failed`` built for ``reason``; a load names the database it loaded into."""
    if failed is LoadFailedError:
        return LoadFailedError(reason, database=location.name)
    return ProvisioningFailedError(reason)


def database_location_from_db_host(db_host: str, name: str) -> DatabaseLocation:
    """``DB_HOST`` (``host`` or ``host:port``) and ``name`` as a location.

    The same parsing ``wp-config.php``'s ``DB_HOST`` gets (database's
    ``Connection.from_db_host``), exposed so ``--archive-database`` is not
    parsed a second way (BL-E002-114). An empty host, a port that is not an
    integer in range, or an invalid database name is a ``UsageError``.
    """
    _require_extra()
    from l3io.wp import database as wpdb

    # ADV-S02-11: fixed texts that echo neither database's message nor the
    # value (an operator's --archive-database is never echoed, L-4).
    try:
        connection = wpdb.Connection.from_db_host(db_host, name)
    except wpdb.InvalidIdentifierError as exc:
        raise UsageError(INVALID_DATABASE_NAME, arguments=("name",)) from exc
    except wpdb.InvalidArgumentsError as exc:
        raise UsageError(INVALID_DB_HOST, arguments=("db_host",)) from exc
    return DatabaseLocation(
        host=connection.host, port=connection.port, name=connection.database
    )


def password_at_call_time(source: CredentialSource) -> Callable[[], str]:
    """A callable resolving ``source``'s password when called, never before.

    ``RestorePorts.cutover_credentials`` for a relocated account: the value is
    read at the cutover's write and registered by ``source`` (a source this
    module built) as it resolves; nothing here keeps it.
    """

    def resolve() -> str:
        return source.resolve().password

    return resolve


def require_wp_config_credentials(source: CredentialSource) -> None:
    """Resolve ``source`` once, so a half-defined pair fails before anything runs.

    Called only where no explicit spec was given (FR-1), so ``source`` is the
    environment-then-``wp-config.php`` chain. The result is discarded;
    ``dump`` resolves again from the source. Raises ``CredentialsError``
    naming the source label and, for a half pair, the missing key or
    variable — never a value.
    """
    _require_extra()
    from l3io.wp import database as wpdb

    label = source.source
    try:
        source.resolve()
    except wpdb.MissingDependencyError as exc:
        raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
    except wpdb.ConfigUnreadableError as exc:
        raise CredentialsError(label, reason="wp-config.php is unreadable") from exc
    except wpdb.CredentialsUnobtainableError as exc:
        # A half-defined wp-config.php pair names its key; a half-set
        # environment pair (which stops the chain before wp-config.php) names
        # the unset variable. Names only, never a value.
        cause = exc.__cause__
        key = (
            cause.key
            if isinstance(cause, wpdb.MissingConfigValueError)
            else unset_partner(os.environ, "wordpress")
        )
        raise CredentialsError(label, missing_key=key) from exc
    except wpdb.CredentialsAbsentError as exc:
        raise CredentialsError(
            label,
            reason=(
                "defines neither DB_USER nor DB_PASSWORD, and neither "
                f"{WORDPRESS_USER_VAR} nor {WORDPRESS_PASSWORD_VAR} is set"
            ),
        ) from exc


def _url_constants(config_file: Path, label: str) -> tuple[str | None, str | None]:
    """``WP_SITEURL`` and ``WP_HOME``, or ``None`` where not defined.

    Database's ``WpConfigFile`` exposes neither, so they are read through
    ``adapters/config_writer.read_defined`` — the package's one importer of
    ``l3io-wordpress-config``, the reader database itself delegates to.
    """
    values = read_defined(config_file, ("WP_SITEURL", "WP_HOME"), label=label)
    site_url, home_url = (
        None if v is None else str(v) for v in (values["WP_SITEURL"], values["WP_HOME"])
    )
    return site_url, home_url


# --------------------------------------------------------------------------
# Credential sources — built by adapters/credential_providers.py's builders,
# registered here; the two modules that construct database's credential classes
# --------------------------------------------------------------------------


class _RegisteringSource:
    """A ``CredentialSource`` that registers each password it resolves.

    Registration happens immediately after the wrapped ``resolve()``, so a
    password is known to the Run's registry before anything can emit it, and
    the winning source's label (never its value) is logged as
    ``credential_source_selected``. A missing extra (``boto3`` for AWS) is
    ``MissingExtraError`` naming it, and an AWS secret with no known region is
    ``CredentialsError`` naming ``region_flag``: database folds both into a
    finding that keeps neither, so they are mapped here, where every source
    resolves. A password shorter than ``MIN_SECRET_LENGTH`` is refused with
    ``CredentialTooShortError`` (ADR-0018 amendment), after it is offered to
    the registry. A failure that is not one of database's own credential errors
    is re-raised as ``CredentialsError`` naming the label and the failure
    class; its text is untrusted, so it is not chained.
    """

    __slots__ = ("_inner", "_region_flag", "_secrets")

    def __init__(
        self, inner: CredentialSource, secrets: SecretRegistry, region_flag: str
    ) -> None:
        self._inner = inner
        self._secrets = secrets
        self._region_flag = region_flag

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
        except wpdb.MissingDependencyError as exc:
            raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
        except wpdb.RegionNotKnownError as exc:
            reason = (
                f"no AWS region: pass {self._region_flag} "
                "(AwsSecretCredentialSpec.region) or set AWS_DEFAULT_REGION"
            )
            raise CredentialsError(label, reason=reason) from exc
        except (wpdb.DatabaseError, BackupError):
            raise
        except Exception as exc:
            failure = type(exc).__name__
        if failure is not None:
            # Raised outside the handler, so not even __context__ holds the
            # untrusted exception.
            raise CredentialsError(label, reason=failure)
        self._secrets.register(resolved.password, source=label)
        if len(resolved.password) < MIN_SECRET_LENGTH:
            # ADR-0018 amendment (BL-E005-005): too short to redact, so
            # refused here, before any connection uses it.
            raise CredentialTooShortError(label, minimum=MIN_SECRET_LENGTH)
        _log.info("credential_source_selected", credential_source=resolved.source)
        return resolved


def _registering(
    source: CredentialSource, secrets: SecretRegistry, *, region_flag: str
) -> CredentialSource:
    """``source`` registering into ``secrets``; re-wrapped if bound to another.

    A re-wrapped source keeps its own ``region_flag``: the inner wrapper maps
    the region error before the outer one sees it.
    """
    if isinstance(source, _RegisteringSource) and source.secrets is secrets:
        return source
    return _RegisteringSource(source, secrets, region_flag)


def wordpress_source(
    spec: CredentialSpec | None,
    *,
    wp_config: CredentialSource,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource:
    """The WordPress account's source, in ``WORDPRESS_CREDENTIAL_PRECEDENCE`` order.

    The spec (explicit or AWS), then the environment, then ``wp-config.php``
    (``credential_providers.wordpress_credentials``).
    """
    _require_extra()
    chain = wordpress_credentials(
        spec, wp_config=wp_config, environ=environ, secrets=secrets
    )
    return _registering(chain, secrets, region_flag=_DB_REGION_FLAG)


def admin_source(
    spec: CredentialSpec | None,
    *,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource | None:
    """The administrator's source, or ``None``: never ``wp-config.php``.

    The spec (explicit or AWS), then the environment
    (``credential_providers.admin_credentials``).
    """
    _require_extra()
    chain = admin_credentials(spec, environ=environ, secrets=secrets)
    if chain is None:
        return None
    return _registering(chain, secrets, region_flag=_ADMIN_REGION_FLAG)


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


def _readiness(verdict: Verdict) -> Readiness:
    blockers, warnings = _codes(verdict)
    return Readiness(ready=verdict.ok, blockers=blockers, warnings=warnings)


def _completed(
    event: str, location: DatabaseLocation, started: int, **outcome: object
) -> None:
    """One ``phase_completed``-shaped record for a restore command (AD-10)."""
    _log.info(
        event,
        db_host=location.host,
        db_port=location.port,
        db_name=location.name,
        duration_ms=(time.monotonic_ns() - started) / _NS_PER_MS,
        **outcome,
    )


class WpDatabase:
    """``DatabaseQuery`` and ``DatabaseCommand``.

    The WordPress ``CredentialSource``, the administrator's when one was given,
    and the Run's ``SecretRegistry`` are bound at construction; no method
    takes a credential. A source not built by :func:`wordpress_source` or
    :func:`admin_source` is wrapped so that what database resolves from it is
    registered all the same.

    Every restore-half method but ``load_dump`` answers as the bound
    administrator and raises ``AdminCredentialsRequiredError`` before any
    connection when none is bound (AD-29). ``load_dump`` loads as the WordPress
    account. Every real command result carries ``performed=True`` (ADR-0022).
    """

    __slots__ = ("_admin", "_admin_tls", "_credentials")

    def __init__(
        self,
        credentials: CredentialSource,
        *,
        secrets: SecretRegistry,
        admin: CredentialSource | None = None,
        admin_tls: AdminTls | None = None,
    ) -> None:
        self._credentials = _registering(
            credentials, secrets, region_flag=_DB_REGION_FLAG
        )
        self._admin = (
            None
            if admin is None
            else _registering(admin, secrets, region_flag=_ADMIN_REGION_FLAG)
        )
        # ``None``: every connection plaintext (the backup root). Set (the
        # restore root): every connection of this instance under the policy.
        self._admin_tls = admin_tls

    def __repr__(self) -> str:
        admin = None if self._admin is None else self._admin.source
        return f"WpDatabase(credentials={self._credentials.source!r}, admin={admin!r})"

    @property
    def administrator_bound(self) -> bool:
        """Whether an administrator source was bound (AD-29)."""
        return self._admin is not None

    def check_dump(self, location: DatabaseLocation) -> Readiness:
        """Database's ``check_dump``, its verdict copied as finding codes."""
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        try:
            verdict = wpdb.check_dump(connection, self._credentials)
        except (
            wpdb.TlsHandshakeError,
            wpdb.TlsVerificationError,
            wpdb.AuthPluginRefusedError,
        ) as exc:
            raise _tls_error(exc, location) from exc
        except wpdb.DatabaseUnreachableError as exc:
            raise DatabaseUnreachableError(location.host, location.port) from exc
        except wpdb.AuthenticationFailedError as exc:
            raise CredentialsError(self._credentials.source) from exc
        except wpdb.AccessDeniedError as exc:
            # An authenticated account lacking a privilege mid-session.
            raise DatabaseRefusedError(findings=(_MISSING_PRIVILEGE,)) from exc
        except wpdb.MissingDependencyError as exc:
            raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
        except wpdb.DatabaseError as exc:
            raise DumpFailedError(type(exc).__name__, database=location.name) from exc
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
            # Structured fields only, never the dependency's text (AD-7), and
            # no chain: database builds the TransferError's message from the
            # tool's first stderr line, untrusted text (N-01).
            code = exc.exit_code
            reason = "the dump stopped" if code is None else f"the dump exited {code}"
            raise DumpFailedError(
                reason, database=location.name, exit_status=code
            ) from None
        except (
            wpdb.TlsHandshakeError,
            wpdb.TlsVerificationError,
            wpdb.AuthPluginRefusedError,
        ) as exc:
            raise _tls_error(exc, location) from exc
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
            raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
        except wpdb.DatabaseError as exc:
            raise DumpFailedError(type(exc).__name__, database=location.name) from exc
        except OSError as exc:
            raise DumpFailedError(
                exc.strerror or type(exc).__name__, database=location.name
            ) from exc
        return DumpResult(bytes=result.bytes, duration_ms=result.duration_ms)

    # ----------------------------------------------------------------------
    # Restore halves (E002-S02-009)
    # ----------------------------------------------------------------------

    def _require_admin(self) -> CredentialSource:
        """AD-29: ``AdminCredentialsRequiredError`` before any connection."""
        if self._admin is None:
            raise AdminCredentialsRequiredError(ADMIN_SOURCE, flags=ADMIN_FLAGS)
        return self._admin

    def survey_target(
        self,
        location: DatabaseLocation,
        *,
        table_prefix: str,
        wordpress_user: str | None,
    ) -> DatabaseTargetState:
        """Database's ``survey`` as the administrator, copied field by field.

        Existence is what the administrator observes, never inferred from the
        WordPress account failing to log in (AD-29).
        """
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        started = time.monotonic_ns()
        with self._mapped(location, admin.source, ProvisioningFailedError):
            survey = wpdb.survey(
                connection,
                admin,
                wordpress_user=wordpress_user,
                user_host=_ANY_HOST,
                table_prefix=table_prefix,
            )
        version = survey.schema_version
        state = DatabaseTargetState(
            database_exists=survey.database_exists,
            table_count=survey.table_count,
            is_wordpress=survey.is_wordpress,
            schema_version=None if version is None else str(version.db_version),
            user_exists=survey.user_exists,
            routine_count=survey.routine_count,
            event_count=survey.event_count,
        )
        _completed(
            "database_surveyed",
            location,
            started,
            observed_by=ADMIN_SOURCE,
            database_exists=state.database_exists,
            table_count=state.table_count,
            routine_count=state.routine_count,
            event_count=state.event_count,
            is_wordpress=state.is_wordpress,
            user_exists=state.user_exists,
        )
        return state

    def check_ensure(self, location: DatabaseLocation) -> Readiness:
        """Database's ``check_ensure``, as finding codes."""
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        with self._mapped(location, admin.source, ProvisioningFailedError):
            verdict = wpdb.check_ensure(
                connection, admin, self._credentials, user_host=_ANY_HOST
            )
        return _readiness(verdict)

    def check_load(
        self, location: DatabaseLocation, *, source: Path | None
    ) -> Readiness:
        """Database's ``check_load`` for the WordPress account; no ``replace``."""
        self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        with self._mapped(location, self._credentials.source, LoadFailedError):
            verdict = wpdb.check_load(connection, self._credentials, source=source)
        return _readiness(verdict)

    def check_revoke(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> Readiness:
        """Database's ``check_revoke``, as finding codes."""
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        with self._mapped(location, admin.source, ProvisioningFailedError):
            verdict = wpdb.check_revoke(
                connection, admin, user, user_host=_ANY_HOST, confirm=confirm
            )
        return _readiness(verdict)

    def check_drop_database(
        self, location: DatabaseLocation, *, confirm: str
    ) -> Readiness:
        """Database's ``check_drop_database``, as finding codes."""
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        with self._mapped(location, admin.source, ProvisioningFailedError):
            verdict = wpdb.check_drop_database(connection, admin, confirm=confirm)
        return _readiness(verdict)

    def ensure_database(self, location: DatabaseLocation) -> EnsureOutcome:
        """Database's ``ensure``: the database, the account, its one grant.

        The grant's database name is escaped by database (AD-28); this
        package writes no ``GRANT`` and quotes no identifier.
        """
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        started = time.monotonic_ns()
        # Database folds an unobtainable WordPress credential into a refusal
        # that names no source; resolving it first lets the error name the
        # WordPress source, not the administrator's (review F1).
        with self._mapped(location, self._credentials.source, ProvisioningFailedError):
            self._credentials.resolve()
        with self._mapped(location, admin.source, ProvisioningFailedError):
            result = wpdb.ensure(
                connection, admin, self._credentials, user_host=_ANY_HOST
            )
        _, warnings = _codes(result.verdict)
        outcome = EnsureOutcome(
            created_database=result.created_database,
            created_user=result.created_user,
            password_set=result.password_set,
            granted=result.granted,
            warnings=warnings,
            performed=True,
        )
        _completed(
            "database_ensured",
            location,
            started,
            created_database=outcome.created_database,
            created_user=outcome.created_user,
            password_set=outcome.password_set,
            granted=outcome.granted,
            warnings=warnings,
        )
        return outcome

    def load_dump(
        self, location: DatabaseLocation, source: IO[bytes] | Path
    ) -> LoadResult:
        """Database's ``load`` as the WordPress account into the empty database.

        ``replace`` is never passed and no decryption argument either: the
        Dump is plain inside the payload (AD-18.3, ADR-0024). A non-empty
        target is refused by database (``database_not_empty``) and raised as
        ``DatabaseRefusedError``; nothing is loaded over it.
        """
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        started = time.monotonic_ns()
        with self._mapped(location, self._credentials.source, LoadFailedError):
            result = wpdb.load(
                connection,
                self._credentials,
                source,
                options=_LOAD_OPTIONS,
            )
        _completed("dump_loaded", location, started, bytes=result.bytes)
        return LoadResult(
            bytes=result.bytes, duration_ms=result.duration_ms, performed=True
        )

    def revoke_grants(
        self, location: DatabaseLocation, *, user: str, confirm: str
    ) -> bool:
        """Database's ``revoke`` on this database only; whether it changed anything."""
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        started = time.monotonic_ns()
        with self._mapped(location, admin.source, ProvisioningFailedError):
            result = wpdb.revoke(
                connection, admin, user, user_host=_ANY_HOST, confirm=confirm
            )
        _completed("grants_revoked", location, started, changed=result.changed)
        return result.changed

    def drop_database(self, location: DatabaseLocation, *, confirm: str) -> bool:
        """Database's ``drop_database``; ``False`` for an absent database.

        Database's warnings (``live_wordpress_site``) are logged, not dropped.
        """
        admin = self._require_admin()
        _require_extra()
        from l3io.wp import database as wpdb

        connection = self._connection(location)
        started = time.monotonic_ns()
        with self._mapped(location, admin.source, ProvisioningFailedError):
            result = wpdb.drop_database(connection, admin, confirm=confirm)
        _, warnings = _codes(result.verdict)
        if warnings:
            _log.warning(
                "database_drop_warnings",
                db_host=location.host,
                db_port=location.port,
                db_name=location.name,
                warnings=warnings,
            )
        _completed(
            "database_dropped",
            location,
            started,
            changed=result.changed,
            warnings=warnings,
        )
        return result.changed

    @contextmanager
    def _mapped(
        self,
        location: DatabaseLocation,
        credential_source: str,
        failed: type[ProvisioningFailedError] | type[LoadFailedError],
    ) -> Iterator[None]:
        """Re-raise database's errors as the package's own (AD-14).

        Structured fields only, never the dependency's text (AD-7):
        ``completed`` steps, an exit code, or the failure class.
        """
        from l3io.wp import database as wpdb

        try:
            yield
        except wpdb.RefusedError as exc:
            raise self._refusal(
                location, exc.verdict, credential_source=credential_source
            ) from exc
        except wpdb.ProvisioningError as exc:
            raise ProvisioningFailedError(
                type(exc).__name__, completed=exc.completed
            ) from exc
        except wpdb.TransferError as exc:
            code = exc.exit_code
            raise LoadFailedError(
                "the load stopped" if code is None else f"the load exited {code}",
                database=location.name,
                exit_status=code,
            ) from exc
        except (wpdb.CorruptDumpError, wpdb.DecryptionError) as exc:
            raise LoadFailedError(type(exc).__name__, database=location.name) from exc
        except (
            wpdb.TlsHandshakeError,
            wpdb.TlsVerificationError,
            wpdb.AuthPluginRefusedError,
        ) as exc:
            raise _tls_error(exc, location) from exc
        except wpdb.DatabaseUnreachableError as exc:
            raise DatabaseUnreachableError(location.host, location.port) from exc
        except (
            wpdb.AuthenticationFailedError,
            wpdb.AccessDeniedError,
            wpdb.CredentialsUnobtainableError,
            wpdb.CredentialsAbsentError,
        ) as exc:
            raise CredentialsError(credential_source) from exc
        except wpdb.MissingDependencyError as exc:
            raise MissingExtraError(extra=_backup_extra(exc.extra)) from exc
        except wpdb.InvalidIdentifierError as exc:
            raise InvalidSiteDescriptorError(_unusable_connection(exc)) from exc
        except wpdb.DatabaseError as exc:
            raise _failure(failed, type(exc).__name__, location) from exc
        except OSError as exc:
            raise _failure(
                failed, exc.strerror or type(exc).__name__, location
            ) from exc

    def _refusal(
        self,
        location: DatabaseLocation,
        verdict: Verdict,
        *,
        credential_source: str | None = None,
    ) -> Exception:
        blockers, _ = _codes(verdict)
        label = credential_source or self._credentials.source
        error = blocked_dump_error(location, blockers, credential_source=label)
        return DatabaseRefusedError(findings=blockers) if error is None else error

    def _connection(self, location: DatabaseLocation) -> Connection:
        """The one place a database ``Connection`` is built from a location.

        With an ``admin_tls`` bound, TLS is ``tls_required_unless_local`` for
        ``location.host`` raised to the override (``UsageError`` below it,
        before any connection); without one, plaintext as before.
        """
        from l3io.wp import database as wpdb

        tls = (
            None
            if self._admin_tls is None
            else _tls_settings(self._admin_tls, location.host)
        )
        try:
            return wpdb.Connection(
                host=location.host,
                database=location.name,
                port=location.port,
                tls=wpdb.TlsSettings() if tls is None else tls,
            )
        except (wpdb.InvalidArgumentsError, wpdb.InvalidIdentifierError) as exc:
            raise InvalidSiteDescriptorError(_unusable_connection(exc)) from exc
