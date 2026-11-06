"""FR-5 — no exclusion rule removes a protected path, ``wp-config.php`` above all.

Scope: a hostile pattern table plus every pattern in ``DEFAULT_EXCLUSIONS``,
each alone and all combined, against every path in ``PROTECTED_PATHS`` — the
set is read from the constant, never retyped. A planted nested protected path
attacks the scope: a guard that only compared the whole path would let a
directory pattern prune the protected file's ancestor.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core import exclusions
from l3io.wp.backup.core.exclusions import (
    DEFAULT_EXCLUSIONS,
    PROTECTED_PATHS,
    ExclusionSet,
)

HOSTILE: tuple[tuple[str, ...], ...] = (
    ("*",),
    ("**",),
    ("**/*",),
    ("*.php",),
    ("wp-config.*",),
    ("/wp-config.php",),
    ("wp-config.php",),
    ("!wp-config.php", "wp-config.php"),
)

PATTERN_SETS: tuple[tuple[str, ...], ...] = (
    *HOSTILE,
    *((p,) for p in DEFAULT_EXCLUSIONS),
    tuple(p for s in HOSTILE for p in s) + DEFAULT_EXCLUSIONS,
    DEFAULT_EXCLUSIONS + tuple(p for s in HOSTILE for p in s),
)


def _sets() -> list[ExclusionSet]:
    built = [ExclusionSet.default(), ExclusionSet.none()]
    for patterns in PATTERN_SETS:
        built.append(ExclusionSet.from_patterns(patterns, include_defaults=False))
        built.append(ExclusionSet.from_patterns(patterns, include_defaults=True))
    return built


def test_the_scope_holds_wp_config() -> None:
    assert "wp-config.php" in PROTECTED_PATHS


@pytest.mark.parametrize("policy", _sets(), ids=lambda s: repr(s.patterns))
def test_wp_config_is_never_excluded(policy: ExclusionSet) -> None:
    decision = policy.decide(PurePosixPath("wp-config.php"), is_dir=False)
    assert not decision.excluded
    assert decision.protected


@pytest.mark.parametrize("policy", _sets(), ids=lambda s: repr(s.patterns))
@pytest.mark.parametrize("protected", sorted(PROTECTED_PATHS))
def test_no_protected_path_is_excluded(policy: ExclusionSet, protected: str) -> None:
    for is_dir in (False, True):
        decision = policy.decide(PurePosixPath(protected), is_dir=is_dir)
        assert not decision.excluded, (protected, policy.patterns)
        assert decision.protected


def test_a_protected_decision_names_the_pattern_that_would_have_matched() -> None:
    policy = ExclusionSet.from_patterns(["*.php"], include_defaults=True)
    decision = policy.decide(PurePosixPath("wp-config.php"), is_dir=False)
    assert (decision.excluded, decision.pattern, decision.protected) == (
        False,
        "*.php",
        True,
    )
    unmatched = ExclusionSet.default().decide(
        PurePosixPath("wp-config.php"), is_dir=False
    )
    assert (unmatched.excluded, unmatched.pattern, unmatched.protected) == (
        False,
        None,
        True,
    )


def test_an_ancestor_of_a_nested_protected_path_is_never_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exclusions, "PROTECTED_PATHS", PROTECTED_PATHS | {"conf/site/secret.php"}
    )
    policy = ExclusionSet.from_patterns(["conf/", "site/", "*"], include_defaults=True)
    for ancestor in ("conf", "conf/site"):
        decision = policy.decide(PurePosixPath(ancestor), is_dir=True)
        assert not decision.excluded, ancestor
        assert decision.protected
    kept = policy.decide(PurePosixPath("conf/site/secret.php"), is_dir=False)
    assert not kept.excluded
    # A sibling, and a file sharing an ancestor's name, are still excluded.
    assert policy.decide(PurePosixPath("conf/other"), is_dir=True).excluded
    assert policy.decide(PurePosixPath("conf/site"), is_dir=False).excluded
