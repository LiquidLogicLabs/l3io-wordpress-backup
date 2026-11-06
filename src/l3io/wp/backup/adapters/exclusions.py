"""FR-5 — the log events of an exclusion set's decisions (AD-10).

``core.exclusions`` is pure and cannot log, so this wrapper does, through the
Run-bound logger (``run_id`` arrives through contextvars): one INFO
``exclusions_applied`` on the first decision, one DEBUG ``path_excluded`` per
excluded decision, and one WARNING ``protected_path_kept`` when a protected
path's decision names the pattern that would have excluded it. Path and
pattern text is ``repr()``-escaped, as member names are.

Satisfies ``ports.exclusion.ExclusionPolicy`` structurally and returns the
wrapped set's decisions unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import structlog

from l3io.wp.backup.core.exclusions import ExclusionSet
from l3io.wp.backup.domain.exclusion import ExclusionDecision

__all__ = ["LoggedExclusionPolicy"]

_log = structlog.get_logger(__name__)


def _escaped(text: str) -> str:
    """``repr()``'s escaping, without its quotes."""
    return repr(text)[1:-1]


@dataclass(frozen=True, slots=True)
class LoggedExclusionPolicy:
    """``policy``, with its decisions logged."""

    policy: ExclusionSet
    # The once-flag for ``exclusions_applied``: the only state this holds.
    _announced: list[bool] = field(
        default_factory=list, init=False, repr=False, compare=False
    )

    @property
    def source(self) -> str:
        return self.policy.source

    @property
    def patterns(self) -> tuple[str, ...]:
        return self.policy.patterns

    def decide(self, relative: PurePosixPath, *, is_dir: bool) -> ExclusionDecision:
        decision = self.policy.decide(relative, is_dir=is_dir)
        if not self._announced:
            self._announced.append(True)
            _log.info(
                "exclusions_applied",
                source=str(self.policy.source),
                pattern_count=len(self.policy.patterns),
            )
        if decision.excluded and decision.pattern is not None:
            _log.debug(
                "path_excluded",
                path=_escaped(relative.as_posix()),
                pattern=_escaped(decision.pattern),
            )
        elif decision.protected and decision.pattern is not None:
            _log.warning(
                "protected_path_kept",
                path=_escaped(relative.as_posix()),
                pattern=_escaped(decision.pattern),
            )
        return decision
