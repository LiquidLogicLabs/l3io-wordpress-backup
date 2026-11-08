"""FR-5 — ``core.exclusions``: the default set, the decision, and its inputs.

Pure: no filesystem. ``wp-config.php``'s protection is pinned in
``tests/contracts/test_wp_config_never_excluded.py``.
"""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.core.exclusions import (
    DEFAULT_EXCLUSIONS,
    ExclusionSet,
    InvalidExclusionPatternError,
    enumerate_defaults,
)
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MAX_BYTES,
    Manifest,
    ManifestExclusions,
    ManifestPayload,
    manifest_to_json,
)
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exclusion import (
    ExcludedEntry,
    ExclusionDecision,
    ExclusionSource,
)
from l3io.wp.backup.domain.exit_codes import ExitCode

#: What each default is there to exclude, and whether it is a directory.
INTENDED = {
    "wp-content/cache/": ("wp-content/cache", True),
    "wp-content/upgrade/": ("wp-content/upgrade", True),
    "wp-content/upgrade-temp-backup/": ("wp-content/upgrade-temp-backup", True),
    "wp-content/debug.log": ("wp-content/debug.log", False),
}

#: Fixed sample paths under the directories the defaults must never touch.
KEPT = [
    ("wp-content/uploads", True),
    ("wp-content/uploads/2026/11/a.jpg", False),
    ("wp-content/uploads/cache", True),
    ("wp-content/uploads/debug.log", False),
    ("wp-content/plugins", True),
    ("wp-content/plugins/woo/cache", True),
    ("wp-content/plugins/woo/debug.log", False),
    ("wp-content/plugins/woo/upgrade", True),
    ("wp-content/themes", True),
    ("wp-content/themes/twenty/style.css", False),
    ("wp-content/themes/twenty/cache", True),
    ("wp-content", True),
    ("index.php", False),
]


def _decide(policy: ExclusionSet, path: str, *, is_dir: bool) -> ExclusionDecision:
    return policy.decide(PurePosixPath(path), is_dir=is_dir)


def test_every_default_has_an_intended_path() -> None:
    assert set(INTENDED) == set(DEFAULT_EXCLUSIONS)


@pytest.mark.parametrize("pattern", DEFAULT_EXCLUSIONS)
def test_each_default_excludes_its_intended_path(pattern: str) -> None:
    path, is_dir = INTENDED[pattern]
    decision = _decide(ExclusionSet.default(), path, is_dir=is_dir)
    assert decision == ExclusionDecision(
        excluded=True, pattern=pattern, protected=False
    )


@pytest.mark.parametrize(("path", "is_dir"), KEPT)
def test_the_defaults_keep_uploads_plugins_and_themes(
    path: str, *, is_dir: bool
) -> None:
    decision = _decide(ExclusionSet.default(), path, is_dir=is_dir)
    assert decision == ExclusionDecision(excluded=False, pattern=None, protected=False)


def test_a_directory_pattern_does_not_exclude_a_file_of_that_name() -> None:
    decision = _decide(ExclusionSet.default(), "wp-content/cache", is_dir=False)
    assert not decision.excluded


def test_a_directory_decision_prunes_the_directory_itself() -> None:
    """The traversal asks about the directory, not its contents."""
    decision = _decide(ExclusionSet.default(), "wp-content/cache", is_dir=True)
    assert decision.excluded
    assert decision.pattern == "wp-content/cache/"


def test_a_negation_re_includes_a_default() -> None:
    policy = ExclusionSet.from_patterns(
        ["!wp-content/debug.log"], include_defaults=True
    )
    assert policy.patterns == (*DEFAULT_EXCLUSIONS, "!wp-content/debug.log")
    assert not _decide(policy, "wp-content/debug.log", is_dir=False).excluded
    assert _decide(policy, "wp-content/cache", is_dir=True).excluded


def test_patterns_are_anchored_at_the_site_root() -> None:
    policy = ExclusionSet.from_patterns(["/old/", "*.log"], include_defaults=False)
    assert _decide(policy, "old", is_dir=True).excluded
    assert not _decide(policy, "wp-content/old", is_dir=True).excluded
    assert _decide(policy, "wp-content/plugins/x/error.log", is_dir=False).excluded


def test_matching_is_case_sensitive_and_separators_are_not_translated() -> None:
    """A backslash escapes, as in gitignore: ``wp-content\\cache`` is literal text."""
    policy = ExclusionSet.from_patterns(
        ["WP-CONTENT/cache/", "wp-content\\cache"], include_defaults=False
    )
    assert not _decide(policy, "wp-content/cache", is_dir=True).excluded
    assert not _decide(policy, "wp-content/cache", is_dir=False).excluded
    assert not _decide(policy, "wp-content\\cache", is_dir=False).excluded
    assert _decide(policy, "wp-contentcache", is_dir=False).excluded


@pytest.mark.parametrize(
    ("patterns", "include_defaults", "expected", "source"),
    [
        ((), True, DEFAULT_EXCLUSIONS, ExclusionSource.DEFAULT),
        ((), False, (), ExclusionSource.NONE),
        (("*.bak",), True, (*DEFAULT_EXCLUSIONS, "*.bak"), ExclusionSource.CUSTOM),
        (("*.bak",), False, ("*.bak",), ExclusionSource.CUSTOM),
    ],
)
def test_from_patterns_puts_the_defaults_first(
    patterns: tuple[str, ...],
    *,
    include_defaults: bool,
    expected: tuple[str, ...],
    source: ExclusionSource,
) -> None:
    policy = ExclusionSet.from_patterns(patterns, include_defaults=include_defaults)
    assert policy.patterns == expected
    assert policy.source is source


def test_from_patterns_refuses_a_bare_string() -> None:
    with pytest.raises(TypeError):
        ExclusionSet.from_patterns("*.log", include_defaults=False)


def test_the_constructors() -> None:
    assert ExclusionSet.default().patterns == DEFAULT_EXCLUSIONS
    assert ExclusionSet.default().source == "default"
    assert ExclusionSet.none().patterns == ()
    assert ExclusionSet.none().source == "none"
    assert not _decide(ExclusionSet.none(), "wp-content/cache", is_dir=True).excluded
    assert enumerate_defaults() == DEFAULT_EXCLUSIONS


@pytest.mark.parametrize(
    "pattern",
    ["", "   ", "\t", "!", "\\", "a\\", "a\nb", "#cache", "/", "["],
)
def test_an_invalid_pattern_is_a_usage_error_at_construction(pattern: str) -> None:
    with pytest.raises(InvalidExclusionPatternError) as caught:
        ExclusionSet.from_patterns(["*.bak", pattern], include_defaults=True)
    assert caught.value.pattern == pattern
    assert caught.value.reason
    assert isinstance(caught.value, UsageError)
    assert type(caught.value).exit_code is ExitCode.USAGE


def test_direct_construction_validates_too() -> None:
    with pytest.raises(InvalidExclusionPatternError):
        ExclusionSet(("",), ExclusionSource.CUSTOM)
    with pytest.raises(TypeError):
        ExclusionSet("*.log", ExclusionSource.CUSTOM)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bogus"):
        ExclusionSet((), "bogus")  # type: ignore[arg-type]
    assert ExclusionSet((), "none").source is ExclusionSource.NONE  # type: ignore[arg-type]


def test_an_escaped_hash_is_a_pattern_not_a_comment() -> None:
    policy = ExclusionSet.from_patterns(["\\#private/"], include_defaults=False)
    assert _decide(policy, "#private", is_dir=True).excluded


@pytest.mark.parametrize("path", ["", ".", "/etc/passwd", "../x", "a/../b", "a/.."])
def test_a_path_that_is_not_under_the_site_root_is_a_programming_error(
    path: str,
) -> None:
    with pytest.raises(ValueError, match="relative path under the Site Root"):
        _decide(ExclusionSet.default(), path, is_dir=False)


def test_an_exclusion_set_is_frozen() -> None:
    policy = ExclusionSet.default()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.patterns = ()  # type: ignore[misc]
    assert policy == ExclusionSet.default()
    assert hash(policy) == hash(ExclusionSet.default())


def test_fifty_thousand_excluded_entries_stay_under_the_manifest_cap() -> None:
    """Top-most paths only: 50,000 individually matched files, one entry each."""
    excluded = tuple(
        ExcludedEntry(
            path=f"wp-content/plugins/plugin-{i // 100:04d}/logs/debug-{i:05d}.log",
            pattern="*.log",
            is_dir=False,
        )
        for i in range(50_000)
    )
    manifest = Manifest(
        format_version=1,
        created_at="2026-11-06T00:00:00Z",
        backup_scope="files",
        source={},
        encrypted=False,
        payload=ManifestPayload(name="payload.tar.gz", size=0, sha256="0" * 64),
        members=(),
        dump_present=False,
        exclusions=ManifestExclusions(
            source="custom", patterns=("*.log",), excluded=excluded
        ),
    )
    size = len(manifest_to_json(manifest).encode("utf-8"))
    assert size < MANIFEST_MAX_BYTES


def test_one_exclusion_source_type_everywhere() -> None:
    """BL-E004-016: the set, the Manifest field and the include-all policy agree."""
    import typing

    from l3io.wp.backup.domain.archive_format import ManifestExclusions
    from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING

    assert INCLUDE_EVERYTHING.source is ExclusionSource.NONE
    assert ExclusionSet.none().source is ExclusionSource.NONE
    hints = typing.get_type_hints(ManifestExclusions)
    assert hints["source"] is ExclusionSource
    assert typing.get_type_hints(ExclusionSet)["source"] is ExclusionSource
