"""AD-9/ADR-0015 — credential precedence and the backup-owned credential spec.

Pure values: what ``cli/``, ``api/`` and ``BackupRequest.credentials`` carry.
``adapters/database.py`` alone turns a spec into a database ``CredentialSource``
(ADR-0015 amendment, 2026-10-27); database's types never appear here or on the
public API.

Two precedence orders, first present wins:

- **WordPress account** — :data:`WORDPRESS_CREDENTIAL_PRECEDENCE`: an explicit
  source, then the environment, then ``wp-config.php``. ``wp-config.php`` is
  always last: it is the Site's own answer, used when nothing overrides it.
- **Administrator account** — :data:`ADMIN_CREDENTIAL_PRECEDENCE`: an explicit
  source, then the environment, and **never** ``wp-config.php``. A provider
  never substitutes the WordPress account for the administrator.

AWS Secrets Manager is a kind of explicit source, not a fourth origin. Epic 5's
providers import these tuples and declare no precedence of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

__all__ = [
    "ADMIN_CREDENTIAL_PRECEDENCE",
    "WORDPRESS_CREDENTIAL_PRECEDENCE",
    "AwsSecretCredentialSpec",
    "CredentialOrigin",
    "CredentialSpec",
    "ExplicitCredentialSpec",
]


class CredentialOrigin(StrEnum):
    """Where a credential pair comes from."""

    EXPLICIT = "explicit"
    ENVIRONMENT = "environment"
    WP_CONFIG = "wp-config"


WORDPRESS_CREDENTIAL_PRECEDENCE: Final = (
    CredentialOrigin.EXPLICIT,
    CredentialOrigin.ENVIRONMENT,
    CredentialOrigin.WP_CONFIG,
)
ADMIN_CREDENTIAL_PRECEDENCE: Final = (
    CredentialOrigin.EXPLICIT,
    CredentialOrigin.ENVIRONMENT,
)


@dataclass(frozen=True, slots=True)
class ExplicitCredentialSpec:
    """A username and password given directly. ``repr`` omits the password."""

    username: str
    password: str

    def __repr__(self) -> str:
        return f"ExplicitCredentialSpec(username={self.username!r})"


@dataclass(frozen=True, slots=True)
class AwsSecretCredentialSpec:
    """A pair held in AWS Secrets Manager; bound by Epic 5's provider."""

    secret_id: str
    region: str | None


CredentialSpec: TypeAlias = ExplicitCredentialSpec | AwsSecretCredentialSpec
