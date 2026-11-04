"""Owner-mandated items must stay owned while work remains, and be closed at the end.

Shawn, 2026-11-03: four MEDIUM red-team findings from the E002 epic closure were
deferred to later epics on the condition that every one is closed before the project
closes out. A condition that lives only in prose gets forgotten, so this test is the
condition:

- **while any planned story is unfinished**, every open gate item must be named, by
  key, in the document of at least one story that is not done — so each item always
  has a live owner, and a story cannot finish while silently dropping one;
- **once every planned story is done**, every gate item must be resolved.

The gate list is ``docs/closeout-gate.yaml`` (the owner's decision record). Issue
status comes from ``state/issues.yaml`` and ``state/issues-resolved.yaml``; story
status comes from every story node under ``state/`` and story text from every story
document — both found by walking, never listed.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "_bmad-output" / "implementation-artifacts"
STATE = IMPL / "state"
GATE = ROOT / "docs" / "closeout-gate.yaml"


def _gate_keys(gate: pathlib.Path = GATE) -> list[str]:
    items = (yaml.safe_load(gate.read_text(encoding="utf-8")) or {}).get("items") or []
    return [str(item["key"]) for item in items]


def _open_issue_keys(state: pathlib.Path = STATE) -> set[str]:
    doc = yaml.safe_load((state / "issues.yaml").read_text(encoding="utf-8")) or {}
    return {str(i["key"]) for i in doc.get("backlog") or []}


def _resolved_issue_keys(state: pathlib.Path = STATE) -> set[str]:
    path = state / "issues-resolved.yaml"
    if not path.is_file():
        return set()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(i["key"]) for i in doc.get("resolved") or []}


def _story_statuses(state: pathlib.Path = STATE) -> dict[str, str]:
    """Every story node in the state tree, keyed by story key."""
    statuses: dict[str, str] = {}
    for node in state.glob("*/epic-*/sprint-*/E*-S*-*.yaml"):
        doc = yaml.safe_load(node.read_text(encoding="utf-8")) or {}
        statuses[str(doc.get("key", node.stem))] = str(doc.get("status", ""))
    return statuses


def _story_texts(impl: pathlib.Path = IMPL) -> dict[str, str]:
    return {
        doc.stem: doc.read_text(encoding="utf-8")
        for doc in impl.glob("epic-*/sprint-*/stories/E*-S*-*.md")
    }


def gate_violations(
    gate_keys: list[str],
    open_keys: set[str],
    resolved_keys: set[str],
    statuses: Mapping[str, str],
    texts: Mapping[str, str],
) -> list[str]:
    """The rule itself, over plain values, so planted cases can exercise it."""
    problems: list[str] = []
    unfinished = {key for key, status in statuses.items() if status != "done"}
    for key in gate_keys:
        if key in resolved_keys and key not in open_keys:
            continue
        if key not in open_keys:
            problems.append(f"{key} is in neither issues.yaml nor issues-resolved.yaml")
            continue
        if not unfinished:
            problems.append(
                f"{key} is still open and every planned story is done: the owner made "
                f"closing it a condition of close-out"
            )
            continue
        owners = sorted(s for s in unfinished if key in texts.get(s, ""))
        if not owners:
            problems.append(
                f"{key} is open but no unfinished story names it, "
                f"so nothing will close it"
            )
    return problems


def test_the_gate_list_is_not_empty() -> None:
    """Scope assertion: an empty gate passes vacuously."""
    assert GATE.is_file(), f"{GATE} is missing"
    assert _gate_keys(), "the close-out gate lists no items"


def test_the_state_tree_has_stories_to_check() -> None:
    """Scope assertion: a walk that finds nothing would make every item 'unowned'."""
    assert _story_statuses(), "no story nodes found under state/"
    assert _story_texts(), "no story documents found"


def test_every_gate_item_is_owned_now_and_closed_at_the_end() -> None:
    problems = gate_violations(
        _gate_keys(),
        _open_issue_keys(),
        _resolved_issue_keys(),
        _story_statuses(),
        _story_texts(),
    )
    assert not problems, "\n".join(problems)


# --- planted cases: each attacks the rule or its scope ---------------------------


def test_planted_an_open_item_no_unfinished_story_names_is_caught() -> None:
    problems = gate_violations(
        ["BL-X-1"],
        {"BL-X-1"},
        set(),
        {"E009-S01-001": "backlog", "E009-S01-002": "done"},
        {"E009-S01-001": "nothing here", "E009-S01-002": "names BL-X-1 but is done"},
    )
    assert problems == [
        "BL-X-1 is open but no unfinished story names it, so nothing will close it"
    ]


def test_planted_an_open_item_at_close_out_is_caught() -> None:
    problems = gate_violations(
        ["BL-X-1"],
        {"BL-X-1"},
        set(),
        {"E009-S01-001": "done"},
        {"E009-S01-001": "names BL-X-1"},
    )
    assert len(problems) == 1
    assert "every planned story is done" in problems[0]


def test_planted_a_gate_key_that_exists_nowhere_is_caught() -> None:
    problems = gate_violations(["BL-X-9"], set(), set(), {"E9-S01-001": "backlog"}, {})
    assert problems == ["BL-X-9 is in neither issues.yaml nor issues-resolved.yaml"]


def test_an_owned_open_item_and_a_resolved_item_both_pass() -> None:
    assert not gate_violations(
        ["BL-X-1", "BL-X-2"],
        {"BL-X-1"},
        {"BL-X-2"},
        {"E009-S01-001": "backlog"},
        {"E009-S01-001": "closes BL-X-1"},
    )
