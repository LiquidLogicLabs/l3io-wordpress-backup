"""FR-14/AD-9 — the credential chains, in the domain's declared precedence.

Layer: ``adapters`` (AD-1). It imports ``domain/`` and ``adapters/redaction``;
``l3io.wp.database`` (the optional ``database`` extra) is imported **inside the
functions that use it** and, for annotations, under ``TYPE_CHECKING`` (AD-8).

Two builders put database's own sources in order and return a database
``CredentialChain`` (first source with an answer wins; a source that *failed*
stops the chain). They write no chain and no source of their own, call no
``resolve()`` and do no I/O:

- :func:`wordpress_credentials` — ``WORDPRESS_CREDENTIAL_PRECEDENCE``: the
  spec (``Explicit`` or ``AwsSecretsManager``), then the environment
  (:data:`WORDPRESS_USER_VAR`/:data:`WORDPRESS_PASSWORD_VAR`), then
  ``wp-config.php``.
- :func:`admin_credentials` — ``ADMIN_CREDENTIAL_PRECEDENCE``: the spec, then
  the environment (:data:`ADMIN_USER_VAR`/:data:`ADMIN_PASSWORD_VAR`), and never
  ``wp-config.php``. ``None`` when there is no administrator source at all.

The environment step is included when either of its two variables is present
in ``environ``; database's ``Environment`` then reads ``os.environ`` when the
chain resolves, so the composition root passes ``os.environ``. A variable set
to ``""`` counts as set, and a password shorter than
``adapters/redaction.MIN_SECRET_LENGTH`` (the empty one included) is accepted
with WARNING ``credential_below_redaction_floor``, logged by
``adapters/database.py``'s wrapper when the chain resolves (owner decision 61,
amending ADR-0018). The one built exception is
``cli/credentials.py::admin_spec``'s ``--admin-password`` fallback, which
reads an empty ``L3IO_WP_BACKUP_ADMIN_PASSWORD`` as unset. One
variable of a pair set
without its partner is a failure, never an absence, so it does not fall
through to ``wp-config.php``.

An explicit spec's password is registered with the Run's ``SecretRegistry``
when its source is built, and a short one warned of there; every password the
chain resolves is registered by ``adapters/database.py``'s wrapper, the only
caller of these builders.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Literal

import structlog

from l3io.wp.backup.adapters.redaction import (
    MIN_SECRET_LENGTH,
    note_below_redaction_floor,
)
from l3io.wp.backup.domain.credentials import (
    ADMIN_CREDENTIAL_PRECEDENCE,
    WORDPRESS_CREDENTIAL_PRECEDENCE,
    AwsSecretCredentialSpec,
    CredentialOrigin,
    CredentialSpec,
)

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

    from l3io.wp.backup.adapters.redaction import SecretRegistry

__all__ = [
    "ADMIN_PASSWORD_VAR",
    "ADMIN_USER_VAR",
    "WORDPRESS_PASSWORD_VAR",
    "WORDPRESS_USER_VAR",
    "admin_credentials",
    "unset_partner",
    "wordpress_credentials",
]

_log = structlog.get_logger(__name__)

#: The WordPress account's variables. Database's ``DB_USER``/``DB_PASSWORD``
#: are deliberately not used: an unrelated tool may have set them.
WORDPRESS_USER_VAR: Final[str] = "L3IO_WP_BACKUP_DB_USER"
WORDPRESS_PASSWORD_VAR: Final[str] = "L3IO_WP_BACKUP_DB_PASSWORD"  # noqa: S105 - a name
#: The administrator account's variables.
ADMIN_USER_VAR: Final[str] = "L3IO_WP_BACKUP_ADMIN_USER"
ADMIN_PASSWORD_VAR: Final[str] = "L3IO_WP_BACKUP_ADMIN_PASSWORD"  # noqa: S105 - a name

_Role = Literal["wordpress", "administrator"]
_PAIRS: Final[Mapping[_Role, tuple[str, str]]] = {
    "wordpress": (WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR),
    "administrator": (ADMIN_USER_VAR, ADMIN_PASSWORD_VAR),
}


def unset_partner(environ: Mapping[str, str], role: _Role) -> str | None:
    """The unset variable of ``role``'s pair when only its partner is set."""
    user, password = _PAIRS[role]
    if (user in environ) == (password in environ):
        return None
    return password if user in environ else user


def _explicit_step(
    spec: CredentialSpec | None, secrets: SecretRegistry
) -> CredentialSource | None:
    if spec is None:
        return None
    from l3io.wp import database as wpdb

    if isinstance(spec, AwsSecretCredentialSpec):
        return wpdb.AwsSecretsManager(spec.secret_id, spec.region)
    secrets.register(spec.secret, source="explicit")
    if len(spec.secret) < MIN_SECRET_LENGTH:
        # Owner decision 61 (amending ADR-0018): accepted, with a WARNING.
        note_below_redaction_floor("explicit")
    # Handed to l3io-wordpress-database: the one place it is revealed here.
    return wpdb.Explicit(spec.username, spec.secret.reveal())


def _environment_step(
    environ: Mapping[str, str], role: _Role
) -> CredentialSource | None:
    user, password = _PAIRS[role]
    if user not in environ and password not in environ:
        return None
    from l3io.wp import database as wpdb

    return wpdb.Environment(user, password)


def _ordered(
    role: _Role,
    precedence: tuple[CredentialOrigin, ...],
    steps: Mapping[CredentialOrigin, CredentialSource | None],
) -> tuple[CredentialSource, ...]:
    """The present steps in ``precedence`` order, logged by label.

    Every origin in ``precedence`` must have a step (``None`` for absent), so
    an origin the domain adds without a builder step fails here, loudly.
    """
    ordered = tuple(s for s in (steps[o] for o in precedence) if s is not None)
    sources = tuple(s.source for s in ordered)
    if ordered:
        _log.debug("credential_chain_built", role=role, sources=sources)
    else:
        _log.debug(
            "credential_chain_built", role=role, sources=sources, none_configured=True
        )
    return ordered


def wordpress_credentials(
    spec: CredentialSpec | None,
    *,
    wp_config: CredentialSource,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource:
    """The WordPress account's chain; ``wp_config`` is always its last step."""
    from l3io.wp import database as wpdb

    ordered = _ordered(
        "wordpress",
        WORDPRESS_CREDENTIAL_PRECEDENCE,
        {
            CredentialOrigin.EXPLICIT: _explicit_step(spec, secrets),
            CredentialOrigin.ENVIRONMENT: _environment_step(environ, "wordpress"),
            CredentialOrigin.WP_CONFIG: wp_config,
        },
    )
    return wpdb.CredentialChain(*ordered)


def admin_credentials(
    spec: CredentialSpec | None,
    *,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> CredentialSource | None:
    """The administrator's chain, or ``None`` when no source is configured.

    There is no ``wp-config.php`` step and no WordPress account step, for any
    input (AD-29): a missing administrator is refused where one is needed.
    """
    ordered = _ordered(
        "administrator",
        ADMIN_CREDENTIAL_PRECEDENCE,
        {
            CredentialOrigin.EXPLICIT: _explicit_step(spec, secrets),
            CredentialOrigin.ENVIRONMENT: _environment_step(environ, "administrator"),
        },
    )
    if not ordered:
        return None
    from l3io.wp import database as wpdb

    return wpdb.CredentialChain(*ordered)
