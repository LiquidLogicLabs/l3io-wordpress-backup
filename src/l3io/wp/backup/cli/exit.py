"""FR-21 — mapping a failure to its exit code, and saying so on stderr.

:func:`exit_code_for` reads the failure's class attribute
(``BackupError.exit_code``, ``domain/errors.py``); nothing here matches over
error classes, so a new error class needs no edit in ``cli/``. A
non-``BackupError`` reaching the root is a defect: it exits ``FAILED`` and is
logged with its traceback (``exc_info``, redacted by the pipeline's
registry). The log is the one place its text appears: what is printed never
carries a non-``BackupError``'s message — a dependency's text is untrusted.

:func:`render_failure` builds the stderr text — the cause and the code's
meaning from :data:`~l3io.wp.backup.domain.exit_codes.DESCRIPTIONS`, plus, at
DEBUG, the traceback with every non-``BackupError`` message in its chain
withheld — and passes all of it through the Run's ``redact`` (ADR-0018). The
``command_failed`` record's ``message`` is the one-line form, without the
traceback. :func:`report_failure` is what the root calls: it
re-opens the Run's ``SecretRegistry`` for the pipeline while it logs, and a
failure while reporting (a closed stderr) never changes the code returned.

``KeyboardInterrupt`` is deliberately not handled here: exit 130 is the
shell's convention, not a member of the table, and Python's default handling
applies once the Run's ``finally`` blocks have run.
"""

from __future__ import annotations

import argparse
import logging
import traceback
from collections.abc import Callable

from l3io.wp.backup.adapters.redaction import SecretRegistry, open_registry
from l3io.wp.backup.cli.output import emit_failure
from l3io.wp.backup.domain.errors import BackupError, exit_code_of
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

__all__ = ["exit_code_for", "render_failure", "report_failure"]

_PROG = "wpbackup2"
_WITHHELD = "<message withheld: not a package error>"


def exit_code_for(exc: BaseException) -> ExitCode:
    """``type(exc).exit_code`` for a ``BackupError``; ``USAGE`` for argparse's."""
    if isinstance(exc, argparse.ArgumentError):
        return ExitCode.USAGE
    return exit_code_of(exc)


def _debug_enabled() -> bool:
    return logging.getLogger().isEnabledFor(logging.DEBUG)


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and the exceptions it was raised from, oldest first."""
    links: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in links):
        links.append(current)
        current = (
            current.__cause__
            if current.__cause__ is not None
            else None
            if current.__suppress_context__
            else current.__context__
        )
    return links[::-1]


def _frames(exc: BaseException) -> str:
    """``exc``'s traceback, chain included; only a ``BackupError`` says why.

    The frames come from :func:`traceback.format_tb`. A link that is not a
    ``BackupError`` is named by class with its message withheld: a
    dependency's text is untrusted, and only registered secrets are redacted.
    """
    parts: list[str] = []
    for link in _chain(exc):
        detail = str(link) if isinstance(link, BackupError) else _WITHHELD
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(link.__traceback__))
        parts.append(f"{type(link).__name__}: {detail}\n")
    return "".join(parts).rstrip("\n")


def render_failure(
    exc: BaseException,
    *,
    redact: Callable[[str], str],
    with_traceback: bool | None = None,
) -> str:
    """The redacted stderr text for ``exc``: its cause and its code's meaning.

    A ``BackupError``'s message is built from its own fields; any other
    exception is named by class only. The traceback follows at DEBUG (or when
    ``with_traceback`` says so), with every non-``BackupError`` message in it
    withheld.
    """
    code = exit_code_for(exc)
    cause = (
        str(exc)
        if isinstance(exc, BackupError)
        else f"unexpected {type(exc).__name__} (a defect; the log has the traceback)"
    )
    text = (
        f"{_PROG}: error: {cause} [exit {int(code)} {code.name}: {DESCRIPTIONS[code]}]"
    )
    if _debug_enabled() if with_traceback is None else with_traceback:
        text += "\n" + _frames(exc)
    return redact(text)


def report_failure(exc: Exception, *, secrets: SecretRegistry) -> ExitCode:
    """Log and print ``exc`` through ``secrets``; return its exit code.

    The code is decided before anything is written, so an error raised while
    writing (a broken stderr) cannot change it.
    """
    code = exit_code_for(exc)
    try:
        with open_registry(secrets):
            emit_failure(
                "command_failed",
                render_failure(exc, redact=secrets.redact, with_traceback=False),
                stderr_text=render_failure(exc, redact=secrets.redact),
                error=type(exc).__name__,
                secrets=secrets,
                exit_code=int(code),
                exit_reason=code.name,
                exc_info=None if isinstance(exc, BackupError) else exc,
            )
    except Exception:  # noqa: S110 - reporting must not change the code
        pass
    return code
