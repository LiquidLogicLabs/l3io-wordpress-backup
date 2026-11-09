"""ADR-0015 — the composition roots' one credential seam.

Turns flag (or library argument) values into a backup ``CredentialSpec`` and
hands it to ``adapters/database.py``, which alone constructs database's
credential classes. ``cli/backup.py``, Epic 3's ``cli/restore.py`` and Epic 6's
``api/`` call this module and construct no database class. The bodies keep
delegating to ``adapters/database.py``, which calls Epic 5's builders
(``adapters/credential_providers.py``) and registers every password they
resolve; delegating to the builders from here would bypass that registration.

:func:`new_db_password` is the relocated account's password chain (owner
decision 2026-11-07): ``--new-db-password``, then ``--new-db-password-file``,
then :data:`~l3io.wp.backup.cli.parser.NEW_DB_PASSWORD_VAR`.

:func:`explicit_setting` turns a secret setting (owner decision 60) into the
explicit step of its chain when it was typed or came from the config file; a
value from its own variable is left to the chain, which reads it there.

**Short database passwords (owner decision 61, amending ADR-0018).** A
database password shorter than ``MIN_SECRET_LENGTH`` is accepted, with WARNING
``credential_below_redaction_floor`` naming its source (never the value):
the registry cannot mask it in logs (:func:`note_below_redaction_floor`).

Error messages name flags and halves, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import structlog

from l3io.wp.backup.adapters import database as _database
from l3io.wp.backup.adapters.credential_providers import ADMIN_PASSWORD_VAR
from l3io.wp.backup.adapters.redaction import (
    MIN_SECRET_LENGTH,
    SecretRegistry,
    note_below_redaction_floor,
)
from l3io.wp.backup.cli.output import RunWarning, WarningSink, emit_warning
from l3io.wp.backup.cli.parser import NEW_DB_PASSWORD_VAR
from l3io.wp.backup.cli.passphrase import note_empty_source
from l3io.wp.backup.cli.settings import flag_name
from l3io.wp.backup.domain.credentials import (
    AwsSecretCredentialSpec,
    CredentialSpec,
    ExplicitCredentialSpec,
    ResolvedCredentialSpec,
)
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.secret import Secret, as_secret
from l3io.wp.backup.ports.database import DbTls

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

__all__ = [
    "NEW_DB_PASSWORD_FILE_FLAG",
    "NEW_DB_PASSWORD_FLAG",
    "admin_source",
    "admin_spec",
    "db_tls_setting",
    "explicit_secret",
    "new_db_password",
    "note_below_redaction_floor",
    "note_password_on_command_line",
    "wordpress_source",
    "wordpress_spec",
]

NEW_DB_PASSWORD_FLAG = "--new-db-password"  # noqa: S105 - a flag name
NEW_DB_PASSWORD_FILE_FLAG = "--new-db-password-file"  # noqa: S105 - a flag name

_log = structlog.get_logger(__name__)

#: The longest password ``--new-db-password-file`` reads, in characters.
PASSWORD_FILE_LIMIT: Final = 4096


def _aws_spec(
    secret_id: str | None, region: str | None, *, prefix: str
) -> AwsSecretCredentialSpec | None:
    if secret_id is None:
        if region is not None:
            msg = (
                f"--{prefix}-aws-region needs --{prefix}-aws-secret-id: "
                "a region names no secret"
            )
            raise UsageError(
                msg, arguments=(f"--{prefix}-aws-region", f"--{prefix}-aws-secret-id")
            )
        return None
    return AwsSecretCredentialSpec(secret_id=secret_id, region=region)


def wordpress_spec(
    *,
    aws_secret_id: str | None,
    aws_region: str | None,
    username: str | None = None,
    password: Secret | str | None = None,
) -> CredentialSpec | None:
    """The WordPress account's spec; ``None`` means the environment, then
    ``wp-config.php``.

    ``username``/``password`` are ``--db-user``/``--db-password`` (or their
    config-file keys): both or neither, and not with an AWS secret.
    """
    aws = _aws_spec(aws_secret_id, aws_region, prefix="db-credentials")
    if username is None and password is None:
        return aws
    if username is None or password is None:
        missing = "--db-password" if password is None else "--db-user"
        msg = f"{missing} is required with its partner: give both halves or neither"
        raise UsageError(msg, arguments=(missing,))
    if aws is not None:
        msg = "give --db-user/--db-password or an AWS secret, not both"
        raise UsageError(
            msg,
            arguments=("--db-user", "--db-password", "--db-credentials-aws-secret-id"),
        )
    return ExplicitCredentialSpec(username=username, password=password)


def explicit_secret(
    value: Secret | str | None, origin: str | None, *, name: str
) -> tuple[Secret | None, str | None]:
    """A secret setting as its chain's explicit step, and the source to name.

    ``origin`` is where the setting ``name`` came from
    (``cli/settings.Origin``). Typed on the command line: its flag, and
    WARNING ``password_on_command_line``. From the config file: ``config file
    key <name>``. From its own variable, or unset: ``(None, None)``, so the
    chain reads the variable itself, as before (owner decision 60).
    """
    if value is None or origin not in ("command line", "config file"):
        return None, None
    if origin == "command line":
        note_password_on_command_line(flag_name(name))
        return as_secret(value), flag_name(name)
    return as_secret(value), f"config file key {name}"


def db_tls_setting(mode: str | None, ca_file: str | Path | None) -> DbTls:
    """``--db-tls``/``--db-tls-ca`` as a ``DbTls`` (owner decision 56)."""
    return DbTls(mode=mode, ca_file=None if ca_file is None else Path(ca_file))


def admin_spec(
    *,
    username: str | None,
    password: Secret | str | None,
    aws_secret_id: str | None,
    aws_region: str | None,
    environ: Mapping[str, str] | None = None,
) -> CredentialSpec | None:
    """The administrator's spec, or ``None`` when none was given.

    Both halves of an explicit pair or neither; an explicit pair or an AWS
    secret, not both. ``--admin-password`` wins; with ``--admin-username``
    and without it, the password comes from ``L3IO_WP_BACKUP_ADMIN_PASSWORD``
    in ``environ`` (RT-S02-002), which keeps it out of the process list.
    With neither flag, nothing here reads the environment: the
    ``L3IO_WP_BACKUP_ADMIN_USER``/``_PASSWORD`` pair is the administrator
    chain's environment step (``adapters/credential_providers``), resolved
    only by a Run that needs an administrator, so a Mode that needs none
    never requires one.
    """
    if password is None and username is not None and environ:
        password = environ.get(ADMIN_PASSWORD_VAR) or None
    if (username is None) != (password is None):
        missing = "--admin-password" if password is None else "--admin-username"
        msg = f"{missing} is required with its partner: give both halves or neither"
        raise UsageError(msg, arguments=(missing,))
    aws = _aws_spec(aws_secret_id, aws_region, prefix="admin")
    if username is not None and password is not None:
        if aws is not None:
            msg = "give --admin-username/--admin-password or an AWS secret, not both"
            raise UsageError(
                msg,
                arguments=(
                    "--admin-username",
                    "--admin-password",
                    "--admin-aws-secret-id",
                ),
            )
        return ExplicitCredentialSpec(username=username, password=password)
    return aws


def wordpress_source(
    spec: CredentialSpec | ResolvedCredentialSpec | None,
    *,
    wp_config: CredentialSource,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource:
    """Delegates to ``adapters.database.wordpress_source``."""
    return _database.wordpress_source(
        spec, wp_config=wp_config, environ=environ, secrets=secrets
    )


def admin_source(
    spec: CredentialSpec | None,
    *,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource | None:
    """Delegates to ``adapters.database.admin_source``."""
    return _database.admin_source(spec, environ=environ, secrets=secrets)


def _read_password_file(path: Path) -> str:
    """The first line of ``path``, its line ending stripped; read once.

    A leading UTF-8 byte-order mark is dropped (``utf-8-sig``), and a first
    line longer than :data:`PASSWORD_FILE_LIMIT` characters is refused rather
    than read whole.
    """
    try:
        with path.open(encoding="utf-8-sig") as handle:
            line = handle.readline(PASSWORD_FILE_LIMIT + 1)
    except (OSError, UnicodeDecodeError) as exc:
        reason = type(exc).__name__
        msg = f"{NEW_DB_PASSWORD_FILE_FLAG}: cannot read {str(path)!r} ({reason})"
        raise UsageError(msg, arguments=(NEW_DB_PASSWORD_FILE_FLAG,)) from None
    password = line.rstrip("\r\n")
    if len(password) > PASSWORD_FILE_LIMIT:
        msg = (
            f"{NEW_DB_PASSWORD_FILE_FLAG}: the first line of {str(path)!r} is "
            f"longer than {PASSWORD_FILE_LIMIT} characters"
        )
        raise UsageError(msg, arguments=(NEW_DB_PASSWORD_FILE_FLAG,))
    return password


def note_password_on_command_line(
    flag: str, on_warning: WarningSink | None = None
) -> None:
    """A password flag's value is in the process list: a notice and a WARNING.

    The notice goes to ``on_warning`` or stderr (owner decision 2026-11-07,
    BL-E008-012); it names the flag, never the value.
    """
    _log.warning("password_on_command_line", flag=flag)
    emit_warning(
        RunWarning(
            code="password_on_command_line",
            message=f"{flag} puts the password in the process list, where other "
            "users can read it; prefer its file or environment variable",
            fields=MappingProxyType({"flag": flag}),
        ),
        on_warning,
    )


def new_db_password(
    *,
    explicit: Secret | str | None,
    file: Path | None,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
    user_given: bool,
    explicit_source: str = NEW_DB_PASSWORD_FLAG,
    on_warning: WarningSink | None = None,
) -> Secret | None:
    """The relocated account's password: the flag, the file, then the variable.

    The first source given wins (the administrator's order: explicit, then
    file, then environment). The variable is read only when ``--new-db-user``
    was given (``user_given``), so one left set in the environment cannot
    turn an ordinary restore into a relocation. ``explicit_source`` names the
    explicit value: the CLI flag logs WARNING ``password_on_command_line``
    naming it, never the value (a library argument is not on a command line).
    The value is registered with ``secrets`` as it
    is read, and one shorter than ``MIN_SECRET_LENGTH`` is accepted with
    WARNING ``credential_below_redaction_floor`` (owner decision 61, amending
    ADR-0018). ``None`` when no source
    gives one. A source that is set but empty counts as none: WARNING
    ``credential_source_empty`` names it and the next source is consulted
    (owner decision 2026-11-07, BL-E008-005).
    """
    source = explicit_source
    value: Secret | str | None = None
    if explicit is not None:
        if explicit:
            value = explicit
            if explicit_source == NEW_DB_PASSWORD_FLAG:
                note_password_on_command_line(NEW_DB_PASSWORD_FLAG, on_warning)
        else:
            note_empty_source(explicit_source)
    if value is None and file is not None:
        source, value = NEW_DB_PASSWORD_FILE_FLAG, _read_password_file(file) or None
        if value is None:
            note_empty_source(NEW_DB_PASSWORD_FILE_FLAG)
    if value is None and user_given and NEW_DB_PASSWORD_VAR in environ:
        source, value = NEW_DB_PASSWORD_VAR, environ[NEW_DB_PASSWORD_VAR] or None
        if value is None:
            note_empty_source(NEW_DB_PASSWORD_VAR)
    if value is None:
        return None
    secret = as_secret(value)
    secrets.register(secret, source=source)
    if len(secret) < MIN_SECRET_LENGTH:
        note_below_redaction_floor(source)
    return secret
