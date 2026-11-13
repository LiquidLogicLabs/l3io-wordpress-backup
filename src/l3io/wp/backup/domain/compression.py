"""Owner decision 88 — the gzip level of an Archive's inner payload stream.

The payload (the inner tar, ADR-0024) is one gzip stream. Its deflate level
is the setting ``compression_level``: a whole number from
:data:`MIN_COMPRESSION_LEVEL` (fastest) to :data:`MAX_COMPRESSION_LEVEL`
(smallest), :data:`DEFAULT_COMPRESSION_LEVEL` unless set. It applies to every
Archive the writer makes, a Safety Snapshot included. Before 1.1.1 it was
``tarfile``'s implicit 9, which nobody had chosen.

The outer stream's level is not this setting: it stays 0, stored blocks
(decision 21; ``adapters/tar.OUTER_COMPRESSLEVEL``). The Archive format is
unchanged: no Manifest field records the level and no reader needs it.

Checked where each spelling is read: the parser's ``type`` (the flag), the
settings model's bounds from these constants (the variable and the config
key), and :func:`check_compression_level` (a library caller, and the writer
itself, so nothing reaches zlib out of range).
"""

from __future__ import annotations

from typing import Final

from l3io.wp.backup.domain.errors import UsageError

__all__ = [
    "COMPRESSION_LEVELS",
    "DEFAULT_COMPRESSION_LEVEL",
    "MAX_COMPRESSION_LEVEL",
    "MIN_COMPRESSION_LEVEL",
    "check_compression_level",
    "compression_level_problem",
]

#: The fastest level a payload may be written at.
MIN_COMPRESSION_LEVEL: Final = 1
#: The smallest-output level: what ``tarfile``'s ``w|gz`` always used.
MAX_COMPRESSION_LEVEL: Final = 9
#: The default (owner decision 88): on a 1.1 GiB WordPress test site, 1.7
#: times faster than 9 for 0.2% more bytes.
DEFAULT_COMPRESSION_LEVEL: Final = 6
#: Every level the setting accepts.
COMPRESSION_LEVELS: Final = range(MIN_COMPRESSION_LEVEL, MAX_COMPRESSION_LEVEL + 1)
_ARGUMENT: Final = "compression_level"


def compression_level_problem(value: object) -> str | None:
    """Why ``value`` is not a level, or ``None`` when it is one.

    A ``bool`` is refused although Python counts it as an ``int``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{value!r} is not a whole number"
    if value not in COMPRESSION_LEVELS:
        return (
            f"{value} is outside {MIN_COMPRESSION_LEVEL}-{MAX_COMPRESSION_LEVEL} "
            f"({MIN_COMPRESSION_LEVEL}: fastest, {MAX_COMPRESSION_LEVEL}: smallest)"
        )
    return None


def check_compression_level(value: object, *, argument: str = _ARGUMENT) -> int:
    """``value`` as a level, or :class:`UsageError` naming ``argument``."""
    problem = compression_level_problem(value)
    if problem is None and isinstance(value, int):
        return value
    msg = f"{argument}: {problem}"
    raise UsageError(msg, arguments=(argument,))
