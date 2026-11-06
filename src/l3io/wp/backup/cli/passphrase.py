"""AD-26 — the one passphrase resolution chain, shared by both composition roots.

First present wins: (1) the library's ``passphrase=`` argument — no CLI flag
takes a passphrase value, so it never reaches ``argv``; (2)
``L3IO_WP_BACKUP_PASSPHRASE`` from the ``environ`` the root passes in (the CLI
passes ``os.environ``; nothing deeper reads the environment). Its exposure:
``/proc/<pid>/environ`` is readable by the same user and by root; (3) a prompt,
which the CLI binds to ``getpass.getpass`` only when stdin is a terminal.

An empty value at any step is absent. The resolved value is registered with
the Run's ``SecretRegistry`` before it is returned, so it is redactable before
``run_started`` and before any request holding it exists. ``api/`` imports
:func:`resolve_passphrase` from here; there is no second chain.

A backup root calls the chain only when passphrase encryption is requested; a
restore root calls it on every restore (ADR-0024), with its own ``label``
saying the passphrase also encrypts the Safety Snapshot and may be left empty
(BL-E003-075).
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

import structlog

from l3io.wp.backup.adapters.redaction import MIN_SECRET_LENGTH, SecretRegistry
from l3io.wp.backup.domain.errors import PassphraseUnavailableError

__all__ = [
    "PASSPHRASE_ENV",
    "ResolvedPassphrase",
    "note_unused_passphrase",
    "require_passphrase",
    "resolve_passphrase",
    "terminal_prompt",
]

PASSPHRASE_ENV: Final = "L3IO_WP_BACKUP_PASSPHRASE"  # noqa: S105 - a variable name
_PROMPT: Final = "Archive passphrase: "

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedPassphrase:
    """A passphrase and which step of the chain supplied it."""

    value: str
    source: Literal["argument", "environment", "prompt"]

    def __repr__(self) -> str:
        return f"ResolvedPassphrase(source={self.source!r})"


def terminal_prompt() -> Callable[[str], str] | None:
    """``getpass.getpass`` when stdin is a terminal, else ``None``."""
    return getpass.getpass if sys.stdin.isatty() else None


def _prompted(prompt: Callable[[str], str], label: str) -> str:
    try:
        return prompt(label)
    except (EOFError, KeyboardInterrupt):
        raise PassphraseUnavailableError(
            PASSPHRASE_ENV, reason="the prompt was closed before a passphrase was given"
        ) from None


def resolve_passphrase(
    *,
    explicit: str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
    secrets: SecretRegistry,
    label: str = _PROMPT,
) -> ResolvedPassphrase | None:
    """The AD-26 chain. ``None`` when no step supplies a passphrase.

    ``label`` is the text ``prompt`` shows.

    Raises ``PassphraseUnavailableError`` when the prompt is closed (EOF or
    interrupt) or the value is shorter than the registry can redact.
    """
    resolved: ResolvedPassphrase | None = None
    if explicit:
        resolved = ResolvedPassphrase(explicit, "argument")
    elif environ.get(PASSPHRASE_ENV):
        resolved = ResolvedPassphrase(environ[PASSPHRASE_ENV], "environment")
    elif prompt is not None:
        value = _prompted(prompt, label)
        if value:
            resolved = ResolvedPassphrase(value, "prompt")
    if resolved is None:
        return None
    if len(resolved.value) < MIN_SECRET_LENGTH:
        raise PassphraseUnavailableError(PASSPHRASE_ENV, reason="too short to protect")
    secrets.register(resolved.value, source=f"passphrase:{resolved.source}")
    return resolved


def require_passphrase(
    *,
    explicit: str | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
    secrets: SecretRegistry,
) -> ResolvedPassphrase:
    """:func:`resolve_passphrase` where encryption was requested: ``None`` refuses.

    A backup root calls this before it reads anything from the Site.
    """
    resolved = resolve_passphrase(
        explicit=explicit, environ=environ, prompt=prompt, secrets=secrets
    )
    if resolved is None:
        reason = "none was given" + (
            "" if prompt is not None else " and no terminal was available to prompt"
        )
        raise PassphraseUnavailableError(PASSPHRASE_ENV, reason=reason)
    return resolved


def note_unused_passphrase(environ: Mapping[str, str]) -> None:
    """One WARNING when the variable is set but no encryption was requested."""
    if environ.get(PASSPHRASE_ENV):
        _log.warning(
            "passphrase_ignored",
            variable=PASSPHRASE_ENV,
            message="encryption was not requested, so the Archive is unencrypted",
        )
