"""AD-24 — the tracing hooks: observation ports, neither query nor command.

Inert in Epic 2: every root binds ``adapters.noop.NoopTracer``. Epic 6's
``adapters/tracing`` implements :class:`Tracer` over OpenTelemetry. Nothing
here imports ``opentelemetry``.

:class:`Redactor` names what a span needs from the Run's ``SecretRegistry``
(``adapters.redaction``), which satisfies it structurally; ``ports/`` may not
import ``adapters/``, so the port names the protocol, not the class.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol

__all__ = ["PhaseSpan", "Redactor", "RunSpan", "SpanAttributes", "Tracer"]

#: Root-span attributes the composition root states (whether the Run writes,
#: and the like). The port carries them opaquely: only ``cli/`` and ``api/``
#: may name the Dry Run (AD-4).
SpanAttributes = Mapping[str, str | int | bool]


class Redactor(Protocol):
    """Replaces a Run's known secrets in text bound for a span attribute."""

    def redact(self, text: str) -> str:
        """``text`` with every registered secret replaced."""
        ...

    def redact_argv(self, argv: Sequence[str]) -> list[str]:
        """Each element of ``argv`` redacted."""
        ...


class RunSpan(Protocol):
    """The root span of one Run."""

    def set_attributes(
        self, *, db_name: str | None = None, archive_bytes: int | None = None
    ) -> None:
        """What is known only after the Run opened: ``db.name``, ``archive.bytes``."""
        ...

    def set_outcome(self, *, exit_code: int, error_type: str | None) -> None:
        """The Run's exit code and, on failure, the error's class name."""
        ...


class PhaseSpan(Protocol):
    """The span of one ``phase_completed`` step."""

    def set_outcome(self, *, exit_code: int, archive_bytes: int | None) -> None:
        """The phase's exit code and the bytes it logged."""
        ...


class Tracer(Protocol):
    """Opens the Run's root span and one span per phase."""

    def run_span(
        self,
        *,
        run_id: str,
        operation: str,
        attributes: SpanAttributes,
        command_args: Sequence[str] | None,
        secrets: Redactor,
    ) -> AbstractContextManager[RunSpan]:
        """The root span with the root's ``attributes``; ``command_args`` is
        redacted through ``secrets``."""
        ...

    def phase_span(
        self, phase: str, *, secrets: Redactor
    ) -> AbstractContextManager[PhaseSpan]:
        """A span named ``phase``, nested in the open root span."""
        ...
