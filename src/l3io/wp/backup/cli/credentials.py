"""ADR-0015 — the composition roots' one credential seam.

Turns flag (or library argument) values into a backup ``CredentialSpec`` and
hands it to ``adapters/database.py``, which alone constructs database's
credential classes. ``cli/backup.py``, Epic 3's ``cli/restore.py`` and Epic 6's
``api/`` call this module and construct no database class. Epic 8
(E008-S01-001) changes only the delegation here, to Epic 5's providers.

Error messages name flags and halves, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from l3io.wp.backup.adapters import database as _database
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.credentials import (
    AwsSecretCredentialSpec,
    CredentialSpec,
    ExplicitCredentialSpec,
)
from l3io.wp.backup.domain.errors import UsageError

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

__all__ = ["admin_source", "admin_spec", "wordpress_source", "wordpress_spec"]


def _aws_spec(
    secret_id: str | None, region: str | None, *, prefix: str
) -> AwsSecretCredentialSpec | None:
    if secret_id is None:
        if region is not None:
            msg = (
                f"--{prefix}-aws-region needs --{prefix}-aws-secret-id: "
                "a region names no secret"
            )
            raise UsageError(msg)
        return None
    return AwsSecretCredentialSpec(secret_id=secret_id, region=region)


def wordpress_spec(
    *, aws_secret_id: str | None, aws_region: str | None
) -> CredentialSpec | None:
    """The WordPress account's spec; ``None`` means ``wp-config.php``."""
    return _aws_spec(aws_secret_id, aws_region, prefix="db-credentials")


def admin_spec(
    *,
    username: str | None,
    password: str | None,
    aws_secret_id: str | None,
    aws_region: str | None,
) -> CredentialSpec | None:
    """The administrator's spec, or ``None`` when none was given.

    Both halves of an explicit pair or neither; an explicit pair or an AWS
    secret, not both.
    """
    if (username is None) != (password is None):
        missing = "--admin-password" if password is None else "--admin-username"
        msg = f"{missing} is required with its partner: give both halves or neither"
        raise UsageError(msg)
    aws = _aws_spec(aws_secret_id, aws_region, prefix="admin")
    if username is not None and password is not None:
        if aws is not None:
            msg = "give --admin-username/--admin-password or an AWS secret, not both"
            raise UsageError(msg)
        return ExplicitCredentialSpec(username=username, password=password)
    return aws


def wordpress_source(
    spec: CredentialSpec | None,
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
