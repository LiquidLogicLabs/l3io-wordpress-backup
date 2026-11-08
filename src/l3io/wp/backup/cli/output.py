"""The CLI's output besides the log: a result on stdout, a failure on stderr.

:func:`emit_result` writes the command's result; a reader that has gone away
(``| head``) does not change the exit code the root returns. :func:`emit_failure`
always leaves one ``error`` record in the structured stream naming the error
class — a failure before the Run opened included — and, unless that stream is
JSON, the plain ``wpbackup2: ...`` line too, so ``--log-format json`` stderr
stays one JSON object per line. The text is redacted here as well as by the
pipeline, whose registry the root re-opens for the report.

:class:`RunWarning` is an operator notice a driver gives besides its WARNING
log record (owner decision 2026-11-07, E006 closure decision 2):
:func:`emit_warning` hands it to the caller's sink when there is one (the
library's ``on_warning``) and otherwise writes its message to stderr, as the
CLI always has. A sink's exception propagates, unchanged, to the caller.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.logging import configured_format

__all__ = ["RunWarning", "WarningSink", "emit_failure", "emit_result", "emit_warning"]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunWarning:
    """An operator notice: what the CLI writes to stderr, as a value.

    ``code`` is the notice's WARNING event name (``archive_unauthenticated``,
    ``safety_snapshot_unencrypted``, ``safety_snapshot_encrypted``),
    ``message`` the line the CLI prints and ``fields`` its structured values
    (paths, the integrity level). It never carries a secret.
    """

    code: str
    message: str
    fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


#: Where a library caller takes notices instead of stderr (``on_warning``).
WarningSink = Callable[[RunWarning], None]


def emit_warning(warning: RunWarning, sink: WarningSink | None) -> None:
    """``warning`` to ``sink``, or its message on stderr when there is none.

    The structured record is the caller's, logged either way, and must be at
    WARNING or above: under ``--log-format json`` it is the notice's only
    trace on stderr. An exception the sink raises propagates.
    """
    if sink is not None:
        sink(warning)
        return
    if configured_format() == "json":
        # stderr stays one JSON object per line (owner decision 2026-11-07,
        # BL-E002-103 UX-5), so nothing is printed here: the notice reaches
        # the operator only through the record the caller logs. That holds
        # only if the caller logs it at WARNING or above, so it survives the
        # default level and --log-level WARNING; every caller of this does.
        return
    print(warning.message, file=sys.stderr)


def emit_result(text: str) -> None:
    """One line on stdout; a closed pipe points stdout at ``/dev/null``."""
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


def emit_failure(
    event: str,
    message: str,
    *,
    error: str,
    secrets: SecretRegistry,
    exc_info: BaseException | None = None,
    stderr_text: str | None = None,
    **fields: object,
) -> None:
    """One ``event`` record at ERROR, and the plain line unless logging is JSON.

    ``stderr_text`` replaces ``message`` on the plain line only (a longer
    form, such as one with a traceback). ``exc_info`` puts a traceback on the
    record; it is redacted only while the Run's registry is open for the
    pipeline (``cli.exit.report_failure``).
    """
    text = secrets.redact(message)
    if exc_info is None:
        _log.error(event, error=error, message=text, **fields)
    else:
        _log.error(event, error=error, message=text, exc_info=exc_info, **fields)
    if configured_format() != "json":
        plain = text if stderr_text is None else secrets.redact(stderr_text)
        print(plain, file=sys.stderr)
