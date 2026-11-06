"""FR-5, AD-10 — ``LoggedExclusionPolicy``'s events, and its decisions unchanged."""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import PurePosixPath
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.core.exclusions import ExclusionSet

#: (path, is_dir) in traversal order.
DECISIONS = [
    ("index.php", False),
    ("wp-config.php", False),
    ("wp-content", True),
    ("wp-content/cache", True),
    ("wp-content/debug.log", False),
    ("wp-content/uploads", True),
    ("wp-content/plugins/x/old.bak", False),
]


def _run(
    policy: ExclusionSet,
) -> tuple[LoggedExclusionPolicy, list[MutableMapping[str, Any]]]:
    logged = LoggedExclusionPolicy(policy)
    with capture_logs() as logs:
        for path, is_dir in DECISIONS:
            got = logged.decide(PurePosixPath(path), is_dir=is_dir)
            assert got == policy.decide(PurePosixPath(path), is_dir=is_dir)
    return logged, logs


def _events(
    logs: list[MutableMapping[str, Any]], name: str
) -> list[MutableMapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


def test_the_events_over_a_run() -> None:
    policy = ExclusionSet.from_patterns(["*.php", "*.bak"], include_defaults=True)
    logged, logs = _run(policy)

    applied = _events(logs, "exclusions_applied")
    assert len(applied) == 1
    assert applied[0]["log_level"] == "info"
    assert applied[0]["source"] == "custom"
    assert applied[0]["pattern_count"] == len(policy.patterns)

    excluded = [
        (p, d)
        for p, d in DECISIONS
        if policy.decide(PurePosixPath(p), is_dir=d).excluded
    ]
    path_excluded = _events(logs, "path_excluded")
    assert [(e["path"], e["pattern"], e["log_level"]) for e in path_excluded] == [
        ("index.php", "*.php", "debug"),
        ("wp-content/cache", "wp-content/cache/", "debug"),
        ("wp-content/debug.log", "wp-content/debug.log", "debug"),
        ("wp-content/plugins/x/old.bak", "*.bak", "debug"),
    ]
    assert len(path_excluded) == len(excluded)

    kept = _events(logs, "protected_path_kept")
    assert [(e["path"], e["pattern"], e["log_level"]) for e in kept] == [
        ("wp-config.php", "*.php", "warning")
    ]
    assert logged.source == "custom"
    assert logged.patterns == policy.patterns


def test_a_protected_decision_without_a_pattern_logs_no_warning() -> None:
    _, logs = _run(ExclusionSet.default())
    assert _events(logs, "protected_path_kept") == []
    assert len(_events(logs, "exclusions_applied")) == 1


def test_each_wrapper_announces_once() -> None:
    policy = ExclusionSet.none()
    _, first = _run(policy)
    _, second = _run(policy)
    assert len(_events(first, "exclusions_applied")) == 1
    assert len(_events(second, "exclusions_applied")) == 1
    assert _events(first, "exclusions_applied")[0]["source"] == "none"


def test_path_text_is_escaped() -> None:
    logged = LoggedExclusionPolicy(
        ExclusionSet.from_patterns(["*.log"], include_defaults=False)
    )
    with capture_logs() as logs:
        logged.decide(PurePosixPath("bad\nname.log"), is_dir=False)
    [event] = _events(logs, "path_excluded")
    assert event["path"] == "bad\\nname.log"


def test_the_wrapper_is_frozen() -> None:
    logged = LoggedExclusionPolicy(ExclusionSet.default())
    with pytest.raises(AttributeError):
        logged.policy = ExclusionSet.none()  # type: ignore[misc]
