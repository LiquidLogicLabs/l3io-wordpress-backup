"""AD-10 — the only logging configuration in the package.

:func:`configure_logging` is called once by the CLI's ``main`` before any Run
opens; Epic 6's ``api/`` calls it only when asked. No other module calls
``structlog.configure``, ``logging.basicConfig``, ``logging.config.*`` or adds
a root handler (``tests/contracts/test_logging_configured_once.py``).

The chain (:func:`processor_chain`), in order: ``merge_contextvars`` first, so
the Run ID and a caller's own ``correlation_id`` reach every record;
``add_log_level``; ``add_logger_name``; an ISO-8601 UTC ``timestamp`` ending
in ``Z``; ``format_exc_info``; the ``trace_processors`` slot (Epic 6 fills it with
``add_trace_ids``; the CLI passes ``()``); E002-S01-006's redaction, as
``adapters.redaction.redact_open_runs`` (the open Run's ``redact_event``); the
renderer. Redaction sits after every enrichment and just before the renderer,
so nothing added late escapes it (ADR-0018).

Stdlib records — ``l3io.wp.config``'s and any third party's — go through
``structlog.stdlib.ProcessorFormatter`` with the same pre-renderer chain, on
one root handler on stderr, so ``l3io.wp.config`` takes no ``structlog``
dependency (AD-2). ``l3io.wp.database`` logs through ``structlog`` and is
covered by the same configuration.

**A database warning's event name.** ``l3io.wp.database`` (0.3.6) logs each
warning finding of a check with its stable code as ``event`` (and in
``code``), the text in ``message``. Up to 1.1.1 this chain renamed 0.3.5's
free-text records to ``database_warning``; with 0.3.6 as the floor that is
redundant and gone (docs/logging.md).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Final, Literal

import structlog
from structlog.typing import Processor

from l3io.wp.backup.adapters.redaction import redact_open_runs

__all__ = [
    "DRY_RUN_KEY",
    "RUN_ID_KEY",
    "configure_logging",
    "configured_format",
    "processor_chain",
    "reset_logging",
]

_HANDLER_NAME: Final = "l3io.wp.backup"

#: The structlog contextvars keys a Run binds (BL-E006-019, owner decision
#: 2026-11-07): prefixed, so a host's own ``run_id``/``dry_run`` is not
#: overwritten for the call.
RUN_ID_KEY: Final = "l3io_run_id"
DRY_RUN_KEY: Final = "l3io_dry_run"

_configured: list[Literal["console", "json"]] = []


def configured_format() -> Literal["console", "json"] | None:
    """The format :func:`configure_logging` resolved; ``None`` when unconfigured."""
    return _configured[-1] if _configured else None


def _renderer(fmt: Literal["console", "json"]) -> Processor:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())


def processor_chain(
    fmt: Literal["console", "json"],
    *,
    trace_processors: Sequence[Processor] = (),
) -> list[Processor]:
    """The whole chain, renderer last and redaction just before it."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        *trace_processors,
        redact_open_runs,
        _renderer(fmt),
    ]


def _level(level: str) -> int:
    value = logging.getLevelNamesMapping().get(level.upper())
    if value is None:
        msg = f"unknown log level {level!r}: not a stdlib level name"
        raise ValueError(msg)
    return value


def _remove_handler(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()


def configure_logging(
    level: str,
    fmt: Literal["console", "json"] | None,
    *,
    trace_processors: Sequence[Processor] = (),
) -> None:
    """Configure structlog and the root handler; a second call replaces both."""
    numeric = _level(level)
    resolved: Literal["console", "json"] = (
        fmt if fmt is not None else ("console" if sys.stderr.isatty() else "json")
    )
    chain = processor_chain(resolved, trace_processors=trace_processors)
    pre_chain, renderer = chain[:-1], chain[-1]
    structlog.configure(
        # filter_by_level first on the structlog side only: a suppressed DEBUG
        # record never runs the chain (stdlib filters foreign records itself).
        processors=[
            structlog.stdlib.filter_by_level,
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=pre_chain,
        )
    )
    root = logging.getLogger()
    _remove_handler(root)
    root.addHandler(handler)
    root.setLevel(numeric)
    _configured[:] = [resolved]


def reset_logging() -> None:
    """Undo :func:`configure_logging`: structlog's defaults, no root handler."""
    _remove_handler(logging.getLogger())
    structlog.reset_defaults()
    _configured.clear()
