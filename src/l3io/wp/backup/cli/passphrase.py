"""AD-26 — the one passphrase resolution chain, shared by both composition roots.

First present wins: (1) an explicit value: the library's ``passphrase=``
argument, or the CLI's ``--passphrase`` or config-file ``passphrase`` (owner
decision 60; ``explicit_source`` names which, and a typed one is in the
process list); (2)
``L3IO_WP_BACKUP_PASSPHRASE`` from the ``environ`` the root passes in (the CLI
passes ``os.environ``; nothing deeper reads the environment). Its exposure:
``/proc/<pid>/environ`` is readable by the same user and by root; (3) a prompt,
which the CLI binds to ``getpass.getpass`` only when stdin is a terminal.

An empty value at any step is absent. The resolved value is registered with
the Run's ``SecretRegistry`` before it is returned, so it is redactable before
``run_started`` and before any request holding it exists. ``api/`` imports
:func:`resolve_passphrase` from here; there is no second chain.

A backup root calls the chain only when passphrase encryption is requested,
through :func:`require_passphrase`, which holds an **encrypting** passphrase
to :data:`MIN_ENCRYPTION_PASSPHRASE_LENGTH` (``PassphraseTooShortError``) and
asks a prompt twice (``PassphraseMismatchError``) (ADR-0024 amendment
2026-11-07, BL-E004-052). Decryption stays permissive: a passphrase that
decrypts (verify, restore) and is shorter than the redaction floor
(``adapters/redaction.MIN_SECRET_LENGTH``) is accepted with WARNING
``credential_below_redaction_floor`` naming its source, never the value
(owner decision 69, as decision 61 for a database password). A restore root calls it
on every restore (ADR-0024), with its own ``label``
saying the passphrase also encrypts the Safety Snapshot and may be left empty
(BL-E003-075).
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import structlog

from l3io.wp.backup.adapters.redaction import (
    MIN_SECRET_LENGTH,
    SecretRegistry,
    note_below_redaction_floor,
)
from l3io.wp.backup.cli.output import RunWarning, WarningSink, emit_warning
from l3io.wp.backup.domain.errors import (
    PassphraseMismatchError,
    PassphraseTooShortError,
    PassphraseUnavailableError,
)
from l3io.wp.backup.domain.secret import Secret, as_secret

__all__ = [
    "MIN_ENCRYPTION_PASSPHRASE_LENGTH",
    "PASSPHRASE_ENV",
    "ResolvedPassphrase",
    "note_empty_source",
    "note_short_restore_passphrase",
    "note_unused_passphrase",
    "require_passphrase",
    "resolve_passphrase",
    "terminal_prompt",
]

PASSPHRASE_ENV: Final = "L3IO_WP_BACKUP_PASSPHRASE"  # noqa: S105 - a variable name
_PROMPT: Final = "Archive passphrase: "
_CONFIRM: Final = "Repeat the passphrase: "
#: The shortest passphrase that may **encrypt** (ADR-0024 amendment, owner
#: decision E004 D5 (a), 2026-11-07), in code points. Decryption has no floor.
MIN_ENCRYPTION_PASSPHRASE_LENGTH: Final = 12

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedPassphrase:
    """A passphrase and which step of the chain supplied it."""

    value: Secret
    source: Literal["argument", "environment", "prompt"]

    def __repr__(self) -> str:
        return f"ResolvedPassphrase(source={self.source!r})"


def terminal_prompt() -> Callable[[str], str] | None:
    """``getpass.getpass`` when stdin is a terminal, else ``None``."""
    return getpass.getpass if sys.stdin.isatty() else None


def _prompted(prompt: Callable[[str], str], label: str, argument: str | None) -> str:
    try:
        return prompt(label)
    except (EOFError, KeyboardInterrupt):
        raise PassphraseUnavailableError(
            PASSPHRASE_ENV,
            reason="the prompt was closed before a passphrase was given",
            argument=argument,
        ) from None


def note_empty_source(source: str) -> None:
    """WARNING ``credential_source_empty``: ``source`` was set but empty.

    ``source`` is a flag, parameter or variable **name**, never a value. The
    chain goes on to its next source (owner decision 2026-11-07, BL-E006-016,
    BL-E008-005).
    """
    _log.warning(
        "credential_source_empty",
        source=source,
        action="ignored; the next source is consulted",
    )


def resolve_passphrase(
    *,
    explicit: Secret | str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
    secrets: SecretRegistry,
    label: str = _PROMPT,
    argument: str | None = None,
    explicit_source: str | None = None,
) -> ResolvedPassphrase | None:
    """The AD-26 chain for a passphrase that decrypts. ``None`` when no step
    supplies one.

    ``explicit_source`` names ``explicit`` when it is not the library's
    argument (``--passphrase``, the config file's key); the registry and an
    empty-source warning name it.

    ``label`` is the text ``prompt`` shows. ``argument`` is the root's name
    for ``explicit`` that a refusal's hint names (the library's
    ``passphrase``); the CLI has none (UX-01-17).

    Raises ``PassphraseUnavailableError`` when the prompt is closed (EOF or
    interrupt). A source that is set but empty is not a passphrase: WARNING
    ``credential_source_empty`` names it, and the chain continues (owner
    decision 2026-11-07, BL-E006-016). A value shorter than the redaction
    floor is accepted, with WARNING ``credential_below_redaction_floor``
    naming its source, never the value: it cannot be masked in logs (owner
    decision 69).
    """
    resolved = _resolve(
        explicit=explicit,
        environ=environ,
        prompt=prompt,
        secrets=secrets,
        label=label,
        argument=argument,
        explicit_source=explicit_source,
    )
    if resolved is not None and len(resolved.value) < MIN_SECRET_LENGTH:
        note_below_redaction_floor(_source_name(resolved, explicit_source, argument))
    return resolved


def _source_name(
    resolved: ResolvedPassphrase, explicit_source: str | None, argument: str | None
) -> str:
    """The flag, parameter, variable or prompt that gave ``resolved``."""
    if resolved.source == "environment":
        return PASSPHRASE_ENV
    if resolved.source == "prompt":
        return "the passphrase prompt"
    return explicit_source or argument or "passphrase"


def _resolve(
    *,
    explicit: Secret | str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
    secrets: SecretRegistry,
    label: str,
    argument: str | None,
    explicit_source: str | None,
) -> ResolvedPassphrase | None:
    """The chain itself, every length accepted; the value is registered."""
    resolved: ResolvedPassphrase | None = None
    if explicit is not None and not explicit:
        note_empty_source(explicit_source or "passphrase")
    if PASSPHRASE_ENV in environ and not environ[PASSPHRASE_ENV] and not explicit:
        note_empty_source(PASSPHRASE_ENV)
    if explicit:
        resolved = ResolvedPassphrase(as_secret(explicit), "argument")
    elif environ.get(PASSPHRASE_ENV):
        resolved = ResolvedPassphrase(Secret(environ[PASSPHRASE_ENV]), "environment")
    elif prompt is not None:
        value = _prompted(prompt, label, argument)
        if value:
            resolved = ResolvedPassphrase(Secret(value), "prompt")
    if resolved is None:
        return None
    named = explicit_source if resolved.source == "argument" else None
    secrets.register(resolved.value, source=f"passphrase:{named or resolved.source}")
    return resolved


def require_passphrase(
    *,
    explicit: Secret | str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
    secrets: SecretRegistry,
    argument: str | None = None,
    explicit_source: str | None = None,
) -> ResolvedPassphrase:
    """The chain where encryption was requested: ``None`` refuses.

    A backup root calls this before it reads anything from the Site. The
    passphrase will encrypt, so it must be at least
    :data:`MIN_ENCRYPTION_PASSPHRASE_LENGTH` characters
    (``PassphraseTooShortError``, however short; owner decision 69 keeps
    this floor), and a prompted one is asked for twice and must match
    (``PassphraseMismatchError``). Both refusals come after the value is
    registered, and neither names it.
    """
    resolved = _resolve(
        explicit=explicit,
        environ=environ,
        prompt=prompt,
        secrets=secrets,
        label=_PROMPT,
        argument=argument,
        explicit_source=explicit_source,
    )
    if resolved is None:
        reason = "none was given" + (
            "" if prompt is not None else " and no terminal was available to prompt"
        )
        raise PassphraseUnavailableError(
            PASSPHRASE_ENV, reason=reason, argument=argument
        )
    if len(resolved.value) < MIN_ENCRYPTION_PASSPHRASE_LENGTH:
        raise PassphraseTooShortError(minimum=MIN_ENCRYPTION_PASSPHRASE_LENGTH)
    if resolved.source == "prompt" and prompt is not None:
        again = Secret(_prompted(prompt, _CONFIRM, argument))
        if again != resolved.value:  # Secret: constant time
            raise PassphraseMismatchError()
    return resolved


def note_short_restore_passphrase(
    resolved: ResolvedPassphrase | None, on_warning: WarningSink | None = None
) -> None:
    """One notice when a restore's passphrase is below the encryption floor.

    A restore accepts any passphrase, so an Archive encrypted before the floor
    existed stays restorable; the same passphrase also encrypts the Safety
    Snapshot, which this says, by length only. The notice goes to
    ``on_warning`` or stderr (owner decision 2026-11-07, BL-E008-012), and the
    WARNING record is logged either way.
    """
    if resolved is not None and len(resolved.value) < MIN_ENCRYPTION_PASSPHRASE_LENGTH:
        message = "the Safety Snapshot is encrypted with this shorter passphrase"
        _log.warning(
            "passphrase_below_encryption_floor",
            minimum=MIN_ENCRYPTION_PASSPHRASE_LENGTH,
            message=message,
        )
        emit_warning(
            RunWarning(
                code="passphrase_below_encryption_floor",
                message=f"passphrase below {MIN_ENCRYPTION_PASSPHRASE_LENGTH} "
                f"characters: {message}",
                fields=MappingProxyType(
                    {"minimum": str(MIN_ENCRYPTION_PASSPHRASE_LENGTH)}
                ),
            ),
            on_warning,
        )


def note_unused_passphrase(
    environ: Mapping[str, str],
    on_warning: WarningSink | None = None,
    *,
    explicit_source: str | None = None,
) -> None:
    """One notice when a passphrase is set but no encryption was requested.

    ``explicit_source`` names a passphrase given as ``--passphrase`` or in the
    config file; otherwise the variable is checked. To ``on_warning`` or
    stderr, with the WARNING record logged either way.
    """
    named = explicit_source or (PASSPHRASE_ENV if environ.get(PASSPHRASE_ENV) else None)
    if named is not None:
        message = "encryption was not requested, so the Archive is unencrypted"
        _log.warning("passphrase_ignored", variable=named, message=message)
        emit_warning(
            RunWarning(
                code="passphrase_ignored",
                message=f"{named} is set but {message}",
                fields=MappingProxyType({"variable": named}),
            ),
            on_warning,
        )
