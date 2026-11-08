"""FR-6, AD-5, AD-17 — the restore vocabulary in ``domain/restore.py``.

FR-6's table, transcribed from the PRD (§5.2) — what each Restore Mode does:

    mode            files  database  clears Site Root  removes database
    files           yes    no        no                no
    files-clean     yes    no        yes               no
    database        no     yes       no                no
    database-clean  no     yes       no                yes
    all             yes    yes       no                no
    all-clean       yes    yes       yes               yes

("files" and "database": whether the Mode restores that part.)

The cases are parametrised from the enum, so a Mode added without a row here
fails rather than going unchecked.
"""

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
from types import ModuleType

import pytest

from l3io.wp.backup.domain.restore import (
    RESTORE_PHASE_ORDER,
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    RestorePart,
    RestorePhase,
    SnapshotPolicy,
    clears_site_root,
    is_subsequence,
    removes_database,
    restores_database,
    restores_files,
)

#: (restores files, restores database, clears Site Root, removes database)
FR6: dict[str, tuple[bool, bool, bool, bool]] = {
    "files": (True, False, False, False),
    "files-clean": (True, False, True, False),
    "database": (False, True, False, False),
    "database-clean": (False, True, False, True),
    "all": (True, True, False, False),
    "all-clean": (True, True, True, True),
}

#: AD-17's order, transcribed.
AD17 = (
    "PREFLIGHT",
    "VERIFY",
    "SNAPSHOT",
    "CLEAN",
    "EXTRACT",
    "REWRITE_CONFIG",
    "LOAD_DUMP",
    "SEARCH_REPLACE",
)


def test_the_modes_are_fr6s_names_verbatim() -> None:
    assert [mode.value for mode in RestoreMode] == list(FR6)


@pytest.mark.parametrize("mode", list(RestoreMode), ids=str)
def test_each_mode_has_fr6s_four_columns(mode: RestoreMode) -> None:
    assert (
        restores_files(mode),
        restores_database(mode),
        clears_site_root(mode),
        removes_database(mode),
    ) == FR6[mode.value]


def test_the_phase_order_is_ad17s() -> None:
    assert tuple(phase.name for phase in RESTORE_PHASE_ORDER) == AD17
    assert tuple(RestorePhase) == RESTORE_PHASE_ORDER


def test_rewrite_config_states_the_cutover_belongs_to_load_dump() -> None:
    doc = RestorePhase.__doc__ or ""
    assert "REWRITE_CONFIG" in doc
    assert "no ``DB_*`` key" in doc
    assert "LOAD_DUMP" in doc


def test_the_full_order_and_its_skips_are_subsequences() -> None:
    assert is_subsequence(RESTORE_PHASE_ORDER)
    assert is_subsequence(())
    for size in range(1, len(RESTORE_PHASE_ORDER)):
        for subset in itertools.combinations(RESTORE_PHASE_ORDER, size):
            assert is_subsequence(subset)


@pytest.mark.parametrize(
    ("first", "second"),
    list(itertools.combinations(RESTORE_PHASE_ORDER, 2)),
    ids=lambda p: p.name,
)
def test_a_reordering_is_rejected(first: RestorePhase, second: RestorePhase) -> None:
    assert not is_subsequence((second, first))


def test_a_repeated_phase_is_rejected() -> None:
    assert not is_subsequence((RestorePhase.EXTRACT, RestorePhase.EXTRACT))


@pytest.mark.parametrize("part", ["files", "database"])
def test_an_override_set_waives_only_what_it_holds(part: RestorePart) -> None:
    assert not OverrideSet.none().waives(part)
    both = OverrideSet(waived=frozenset(Override))
    assert both.waives(part)


def test_the_files_and_database_waivers_are_independent() -> None:
    files = OverrideSet(waived=frozenset({Override.NONEMPTY_FILES}))
    assert files.waives("files")
    assert not files.waives("database")
    database = OverrideSet(waived=frozenset({Override.NONEMPTY_DATABASE}))
    assert database.waives("database")
    assert not database.waives("files")


def test_an_override_set_cannot_name_a_mode() -> None:
    """``OverrideSet`` is a distinct type: it holds overrides, never a Mode."""
    assert not issubclass(OverrideSet, RestoreMode)
    assert {o.value for o in Override}.isdisjoint({m.value for m in RestoreMode})


def test_the_policies_are_closed_two_value_sets() -> None:
    assert [p.value for p in SnapshotPolicy] == ["take", "skip"]
    assert [p.value for p in ConfigPolicy] == ["keep", "overwrite"]


# --------------------------------------------------------------------------
# AD-5 — tools/check_mode_defaults.py guards the restore vocabulary
# --------------------------------------------------------------------------

RESTORE_VALUE_TYPES = ("RestoreMode", "SnapshotPolicy", "ConfigPolicy", "OverrideSet")


def _tool() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "tools" / "check_mode_defaults.py"
    spec = importlib.util.spec_from_file_location("check_mode_defaults", path)
    assert spec is not None
    assert spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def test_the_tool_guards_every_restore_value_type() -> None:
    assert set(RESTORE_VALUE_TYPES) <= _tool().GUARDED_TYPES


def test_the_guarded_set_is_derived_from_the_vocabulary_modules() -> None:
    """AD-10/SD-02-8: the closed choices the old hand list missed are guarded."""
    assert {"RestorePhase", "Override", "RestorePart", "BackupScope"} <= (
        _tool().GUARDED_TYPES
    )


def test_a_closed_choice_added_to_the_vocabulary_is_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Attack the scope: a new enum, a new Literal alias and a holder of one,
    added to a copy of ``domain/restore.py``, are guarded with no edit to the
    tool, and a defaulted parameter of each fails."""
    tool = _tool()
    vocabulary = tmp_path / "restore.py"
    vocabulary.write_text(
        tool.VOCABULARY[0].read_text(encoding="utf-8")
        + "\n\nclass NewChoice(StrEnum):\n    A = 'a'\n"
        + "\nNewPart = Literal['x', 'y']\n"
        + "\n@dataclass(frozen=True)\nclass Holder:\n    parts: frozenset[NewPart]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "VOCABULARY", (vocabulary,))
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir()
    planted.write_text(
        "def a(x: NewChoice = NewChoice.A) -> None: ...\n"
        "def b(*, x: NewPart = 'x') -> None: ...\n"
        "def c(x: Holder | None = None) -> None: ...\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "PACKAGE_ROOT", planted.parent)
    assert tool.main() == 1
    err = capsys.readouterr().err
    assert all(name in err for name in ("NewChoice", "NewPart", "Holder"))


def test_a_missing_vocabulary_module_is_a_scope_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool()
    monkeypatch.setattr(tool, "VOCABULARY", (tmp_path / "gone.py",))
    with pytest.raises(SystemExit, match="scope error"):
        tool.main()


@pytest.mark.parametrize("guarded", sorted(_tool().GUARDED_TYPES))
@pytest.mark.parametrize(
    "source",
    [
        "def restore(x: {t} = DEFAULT) -> None: ...\n",
        "def restore(*, x: {t} | None = None) -> None: ...\n",
        "class Request:\n    x: {t} = DEFAULT\n",
    ],
    ids=["positional", "keyword-only", "class-attribute"],
)
def test_a_planted_default_of_each_guarded_type_fails(
    guarded: str,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The planted file sits in a nested tree the tool was never told about."""
    tool = _tool()
    planted = tmp_path / "deep" / "planted.py"
    planted.parent.mkdir()
    planted.write_text(source.format(t=guarded), encoding="utf-8")
    monkeypatch.setattr(tool, "PACKAGE_ROOT", tmp_path)
    assert tool.main() == 1
    err = capsys.readouterr().err
    assert "planted.py" in err
    assert guarded in err


def test_the_tool_reaches_core_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E003-S01-001: a default planted in a copy of ``core/restore.py`` fails.

    The copy keeps the module's own path under the package tree, so the
    guard is proven to reach the new module, not only a planted file.
    """
    tool = _tool()
    source = Path(tool.PACKAGE_ROOT) / "core" / "restore.py"
    copy = tmp_path / "core" / "restore.py"
    copy.parent.mkdir()
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(tool, "PACKAGE_ROOT", tmp_path)
    assert tool.main() == 0
    capsys.readouterr()
    copy.write_text(
        copy.read_text(encoding="utf-8")
        + "\n\ndef f(mode: RestoreMode = RestoreMode.ALL) -> None: ...\n",
        encoding="utf-8",
    )
    assert tool.main() == 1
    err = capsys.readouterr().err
    assert "restore.py" in err
    assert "RestoreMode" in err
