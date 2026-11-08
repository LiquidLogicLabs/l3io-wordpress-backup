"""ADR-0018 — the one redaction implementation, applied where text leaves the program.

A :class:`SecretRegistry` holds the secrets one Run has seen — every database
password ``adapters/database.py`` resolves and every passphrase
``cli/passphrase.py`` resolves — and replaces them with :data:`REDACTED` in
text bound for a log record, the CLI's stderr or (Epic 6) a span attribute.
It satisfies ``ports/tracing.Redactor`` structurally (``redact``,
``redact_argv``).

Redaction is the **backstop**. The primary control is that no message, log
field or exception ever interpolates a credential in the first place; the
registry catches what that control misses.

Limits, stated because string matching cannot see past them:

- a secret that has been transformed — URL-encoded, base64'd, split across two
  log fields — is not recognised. The guard is upstream: no code path in this
  package builds a URL or connection string holding a credential;
- a secret shorter than :data:`MIN_SECRET_LENGTH` is not registered (it would
  redact unrelated text); it is logged as ``secret_too_short_to_redact`` with
  its source label only;
- ``str(exc)`` or ``repr(exc)`` called directly by a library caller passes
  through nothing here (ADR-0018 "What this does not cover");
- a host that configures logging itself has no :func:`redact_open_runs` in its
  chain.

A field that is neither text, an exception nor a container — ``bytes``, a
``Path``, any object — is examined as the text the renderer would make of it
(``bytes`` decoded as UTF-8 with ``surrogateescape``, anything else ``repr()``);
when that text holds a secret the field becomes the redacted text.

**In the configured pipeline** (``cli/logging.py``, E002-S01-004) the processor
is :func:`redact_open_runs`: it applies :meth:`SecretRegistry.redact_event` of
every registry :func:`open_registry` has made live — ``cli.context.open_run``
does so for the Run's registry for exactly the Run's lifetime. Every open
registry, not only the current context's, so a record from a thread that did
not inherit the Run's context is redacted all the same.

No maintained library redacts *known runtime values* across structlog event
dicts; pattern scrubbers cannot, because a secret's shape is unknown.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final, NoReturn

import structlog
from structlog.processors import format_exc_info
from structlog.typing import EventDict, WrappedLogger

from l3io.wp.backup.domain.secret import Secret

__all__ = [
    "MIN_SECRET_LENGTH",
    "REDACTED",
    "SecretRegistry",
    "open_registry",
    "redact_open_runs",
]

#: The marker database's ``ResolvedCredentials.__repr__`` uses.
REDACTED: Final = "***"
#: Shorter values are not registered: they would redact unrelated text.
MIN_SECRET_LENGTH: Final = 4
_SEQUENCES: Final = (list, tuple, set, frozenset)

_log = structlog.get_logger(__name__)


def _occurrences(text: str, form: str) -> list[int]:
    found, start = [], text.find(form)
    while start != -1:
        found.append(start)
        start = text.find(form, start + 1)
    return found


class SecretRegistry:
    """The secrets one Run must never emit. Thread-safe; never logged or pickled."""

    __slots__ = ("_forms", "_lock", "_secrets")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: frozenset[str] = frozenset()
        #: Every registered value and its escaped spellings (below).
        self._forms: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"SecretRegistry(<{len(self)} secrets>)"

    def __len__(self) -> int:
        return len(self._secrets)

    def __reduce__(self) -> NoReturn:
        msg = "a SecretRegistry is never pickled"
        raise TypeError(msg)

    def register(self, value: str | Secret, *, source: str = "unspecified") -> None:
        """Add ``value``; an empty or too-short one is refused with a WARNING."""
        if isinstance(value, Secret):
            value = value.reveal()
        if len(value) < MIN_SECRET_LENGTH:
            _log.warning("secret_too_short_to_redact", source=source)
            return
        # repr() and JSON escape quotes, backslashes and non-ASCII: text built
        # from an exception's args or a %r log argument holds that spelling.
        forms = {value, repr(value)[1:-1], json.dumps(value)[1:-1]}
        with self._lock:
            self._secrets = self._secrets | {value}
            self._forms = tuple(set(self._forms) | forms)

    def redact(self, text: str) -> str:
        """``text`` with every registered secret replaced by :data:`REDACTED`.

        Every occurrence of every form is found first and overlapping or
        nested spans merged, so neither a secret inside another nor two
        overlapping secrets leave a fragment behind.
        """
        spans = sorted(
            (start, start + len(form))
            for form in self._forms
            for start in _occurrences(text, form)
        )
        if not spans:
            return text
        parts: list[str] = []
        done = 0
        start, end = spans[0]
        for next_start, next_end in spans[1:]:
            if next_start > end:
                parts += (text[done:start], REDACTED)
                done, start = end, next_start
            end = max(end, next_end)
        parts += (text[done:start], REDACTED, text[end:])
        return "".join(parts)

    def redact_argv(self, argv: Sequence[str]) -> list[str]:
        """Each element of ``argv`` redacted."""
        return [self.redact(arg) for arg in argv]

    def redact_event(
        self, logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        """A structlog processor: install last, just before the renderer.

        Formats ``exc_info`` into text first (structlog's own
        ``format_exc_info``), so a traceback is redacted like any other field.
        Emits no record itself.
        """
        if not self._forms:
            return event_dict
        if event_dict.get("exc_info"):
            event_dict = format_exc_info(logger, method_name, event_dict)
        walked = {self.redact(k): self._walk(v) for k, v in event_dict.items()}
        event_dict.clear()
        event_dict.update(walked)
        return event_dict

    def _walk(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, BaseException):
            # Always text: the renderer would repr() the object, args and all.
            return self.redact(f"{type(value).__name__}: {value}")
        if isinstance(value, Mapping):
            return {self._walk(k): self._walk(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            walked = [self._walk(v) for v in value]
            # A named tuple or other subclass comes back as a plain list.
            return type(value)(walked) if type(value) in _SEQUENCES else walked
        if value is None or isinstance(value, bool | int | float):
            return value
        # bytes, a Path or any other object: the renderer stringifies it after
        # this step, so examine that text now. Unchanged unless it holds one.
        if isinstance(value, bytes | bytearray):
            text = bytes(value).decode("utf-8", errors="surrogateescape")
        else:
            text = repr(value)
        redacted = self.redact(text)
        return value if redacted == text else redacted

    def log_registered(self) -> None:
        """The one ``secrets_registered`` DEBUG record a Run emits: a count."""
        _log.debug("secrets_registered", count=len(self))


_OPEN: list[SecretRegistry] = []
_OPEN_LOCK = threading.Lock()


@contextmanager
def open_registry(registry: SecretRegistry) -> Iterator[SecretRegistry]:
    """Make ``registry`` live for :func:`redact_open_runs` while the block runs."""
    with _OPEN_LOCK:
        _OPEN.append(registry)
    try:
        yield registry
    finally:
        with _OPEN_LOCK:
            for i, live in enumerate(_OPEN):
                if live is registry:
                    del _OPEN[i]
                    break


def redact_open_runs(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """A structlog processor: each live registry's ``redact_event``, in turn."""
    with _OPEN_LOCK:
        live = tuple(_OPEN)
    for registry in live:
        event_dict = registry.redact_event(logger, method_name, event_dict)
    return event_dict
