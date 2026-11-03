"""The CLI's output besides the log: a result on stdout, a failure on stderr.

:func:`emit_result` writes the command's result; a reader that has gone away
(``| head``) does not change the exit code the root returns. :func:`emit_failure`
always leaves one ``error`` record in the structured stream naming the error
class — a failure before the Run opened included — and, unless that stream is
JSON, the plain ``wpbackup2: ...`` line too, so ``--log-format json`` stderr
stays one JSON object per line. The text is redacted here: the Run, and with
it the pipeline's registry, has already closed.
"""

from __future__ import annotations

import os
import sys

import structlog

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.logging import configured_format

__all__ = ["emit_failure", "emit_result"]

_log = structlog.get_logger(__name__)


def emit_result(text: str) -> None:
    """One line on stdout; a closed pipe points stdout at ``/dev/null``."""
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


def emit_failure(
    event: str, message: str, *, error: str, secrets: SecretRegistry
) -> None:
    """One ``event`` record at ERROR, and the plain line unless logging is JSON."""
    text = secrets.redact(message)
    _log.error(event, error=error, message=text)
    if configured_format() != "json":
        print(text, file=sys.stderr)
