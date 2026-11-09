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

:class:`ResolvedCredentialSpec` is not a fifth source: it is the WordPress
chain's answer, resolved by a backup the CLI started as root before it
switched to the Site Root's owner (owner decision 67), carried into the Run
so nothing only root could read is read again. It is not part of
:data:`CredentialSpec`, the public type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from l3io.wp.backup.domain.secret import Secret, as_secret

__all__ = [
    "ADMIN_CREDENTIAL_PRECEDENCE",
    "WORDPRESS_CREDENTIAL_PRECEDENCE",
    "AwsSecretCredentialSpec",
    "CredentialOrigin",
    "CredentialSpec",
    "ExplicitCredentialSpec",
    "ResolvedCredentialSpec",
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
    """A username and password given directly. ``repr`` omits the password.

    ``password`` accepts a ``str`` or a :class:`~l3io.wp.backup.domain.secret.Secret`
    and is always held as a ``Secret`` (a ``str`` is wrapped at once).
    """

    username: str
    password: Secret | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "password", as_secret(self.password))

    @property
    def secret(self) -> Secret:
        """``password``, typed as the ``Secret`` it always is."""
        return as_secret(self.password)

    def __repr__(self) -> str:
        return f"ExplicitCredentialSpec(username={self.username!r})"


@dataclass(frozen=True, slots=True)
class AwsSecretCredentialSpec:
    """A pair held in AWS Secrets Manager; bound by Epic 5's provider."""

    secret_id: str
    region: str | None


CredentialSpec: TypeAlias = ExplicitCredentialSpec | AwsSecretCredentialSpec


@dataclass(frozen=True, slots=True)
class ResolvedCredentialSpec:
    """The WordPress chain's answer, resolved before a switch of user.

    ``chain`` is the label of the chain that was resolved (what the plan
    names), ``source`` the label of the step that answered. ``repr`` omits
    the password.
    """

    username: str
    password: Secret | str
    source: str
    chain: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "password", as_secret(self.password))

    @property
    def secret(self) -> Secret:
        """``password``, typed as the ``Secret`` it always is."""
        return as_secret(self.password)

    def __repr__(self) -> str:
        return (
            f"ResolvedCredentialSpec(username={self.username!r}, "
            f"source={self.source!r})"
        )
