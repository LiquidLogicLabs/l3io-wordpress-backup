"""FR-5 — the exclusion set: which Site Root paths stay out of an Archive.

Pure (AD-1): imports ``domain/`` and ``pathspec`` only, and of ``pathspec``
only the string matcher — never its tree-walking helpers, which do I/O.
Patterns are gitignore syntax, anchored at the Site Root and compiled once
with ``pathspec.GitIgnoreSpec``. No rule can exclude a path in
:data:`PROTECTED_PATHS`.

:class:`ExclusionSet` satisfies ``ports.exclusion.ExclusionPolicy``
structurally; this module does not import ``ports/``. It logs nothing — the
events are ``adapters.exclusions.LoggedExclusionPolicy``'s.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from pathspec import GitIgnoreSpec

from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exclusion import ExclusionDecision

__all__ = [
    "DEFAULT_EXCLUSIONS",
    "PROTECTED_PATHS",
    "ExclusionSet",
    "ExclusionSource",
    "InvalidExclusionPatternError",
    "enumerate_defaults",
]

#: The documented default set (``docs/exclusions.md``): object and page
#: caches, core's update scratch directories and WordPress's own debug log.
#: Nothing under ``uploads/``, ``plugins/`` or ``themes/``. Changing it is a
#: reviewed change to this constant and its documentation.
DEFAULT_EXCLUSIONS: Final[tuple[str, ...]] = (
    "wp-content/cache/",
    "wp-content/upgrade/",
    "wp-content/upgrade-temp-backup/",
    "wp-content/debug.log",
)

#: Site-Root-relative POSIX paths no rule may exclude.
PROTECTED_PATHS: Final[frozenset[str]] = frozenset({"wp-config.php"})


class ExclusionSource(StrEnum):
    """Where an :class:`ExclusionSet`'s patterns came from (the Manifest's key)."""

    DEFAULT = "default"
    CUSTOM = "custom"
    NONE = "none"


class InvalidExclusionPatternError(UsageError):
    """An exclusion pattern that cannot be used; exits ``USAGE`` (AD-14)."""

    def __init__(self, pattern: str, reason: str) -> None:
        self.pattern = pattern
        self.reason = reason
        super().__init__(f"invalid exclusion pattern {pattern!r}: {reason}")


def _compile(patterns: tuple[object, ...]) -> GitIgnoreSpec:
    """Each pattern compiled once, so a rejection names the pattern at fault.

    A pattern ``pathspec`` compiles to a no-op (a ``#`` comment, a lone ``/``,
    an unclosed ``[``) is refused rather than listed as active and ignored.
    """
    compiled = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            msg = f"an exclusion pattern must be a str, not {type(pattern).__name__}"
            raise TypeError(msg)
        if not pattern.strip():
            raise InvalidExclusionPatternError(pattern, "the pattern is empty")
        if "\n" in pattern or "\r" in pattern:
            raise InvalidExclusionPatternError(pattern, "the pattern spans lines")
        try:
            [one] = GitIgnoreSpec.from_lines([pattern]).patterns
        except ValueError as exc:
            raise InvalidExclusionPatternError(pattern, str(exc)) from exc
        if one.include is None:
            raise InvalidExclusionPatternError(
                pattern, "the pattern matches nothing (a comment or an empty match)"
            )
        compiled.append(one)
    return GitIgnoreSpec(compiled)


def _validated(relative: PurePosixPath) -> str:
    text = relative.as_posix()
    if relative.is_absolute() or text in {"", "."} or ".." in relative.parts:
        msg = (
            f"an exclusion decision needs a relative path under the Site Root: {text!r}"
        )
        raise ValueError(msg)
    return text


def _protects(text: str, *, is_dir: bool) -> bool:
    """``text`` is a protected path, or a directory holding one."""
    if text in PROTECTED_PATHS:
        return True
    return is_dir and any(
        PurePosixPath(text) in PurePosixPath(p).parents for p in PROTECTED_PATHS
    )


@dataclass(frozen=True, slots=True)
class ExclusionSet:
    """An ordered gitignore pattern list; later patterns win, ``!`` re-includes."""

    patterns: tuple[str, ...]
    source: ExclusionSource
    _spec: GitIgnoreSpec = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Checked at run time too: a library caller is not type-checked.
        raw: object = self.patterns
        if isinstance(raw, str):
            msg = "patterns must be a sequence of patterns, not one string"
            raise TypeError(msg)
        source: object = self.source
        if not isinstance(source, ExclusionSource):
            object.__setattr__(self, "source", ExclusionSource(str(source)))
        patterns = tuple(self.patterns)
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "_spec", _compile(patterns))

    @classmethod
    def default(cls) -> ExclusionSet:
        """:data:`DEFAULT_EXCLUSIONS`."""
        return cls(DEFAULT_EXCLUSIONS, ExclusionSource.DEFAULT)

    @classmethod
    def none(cls) -> ExclusionSet:
        """Excludes nothing."""
        return cls((), ExclusionSource.NONE)

    @classmethod
    def from_patterns(
        cls, patterns: Sequence[str], *, include_defaults: bool
    ) -> ExclusionSet:
        """The defaults (if wanted) first, then ``patterns``.

        So a later ``!pattern`` can re-include a default. With no ``patterns``
        the set is :meth:`default` or :meth:`none`, and says so in ``source``.
        """
        raw: object = patterns
        if isinstance(raw, str):
            msg = "patterns must be a sequence of patterns, not one string"
            raise TypeError(msg)
        given = tuple(patterns)
        if not given:
            return cls.default() if include_defaults else cls.none()
        head = DEFAULT_EXCLUSIONS if include_defaults else ()
        return cls(head + given, ExclusionSource.CUSTOM)

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        """Whether ``relative`` (under the Site Root) stays out of the Archive.

        ``excluded`` on a directory prunes its whole subtree. A protected path
        — or a directory holding one — is never excluded; its decision carries
        the pattern that would have matched, or ``None``.
        """
        text = _validated(relative)
        result = self._spec.check_file(f"{text}/" if is_dir else text)
        pattern = (
            self.patterns[result.index]
            if result.include and result.index is not None
            else None
        )
        if _protects(text, is_dir=is_dir):
            return ExclusionDecision(excluded=False, pattern=pattern, protected=True)
        return ExclusionDecision(
            excluded=pattern is not None, pattern=pattern, protected=False
        )


def enumerate_defaults() -> tuple[str, ...]:
    """The default set, as ``--list-default-excludes`` and ``api/`` print it."""
    return DEFAULT_EXCLUSIONS
