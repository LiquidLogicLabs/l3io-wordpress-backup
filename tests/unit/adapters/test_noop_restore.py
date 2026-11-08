"""AD-4 — the restore no-op twins return their declared type and touch nothing.

Each twin is driven over a temporary Site Root, a temporary journal directory
and a recording database double, which are asserted unchanged; each call
emits one ``phase_completed`` with ``outcome="skipped_dry_run"`` and
``bytes=0`` and is recorded into the ``ActionLog``. A config write records
the **keys** of ``values``, never a value. ``NoopDatabase``'s administrator
methods refuse without one, as the real adapter does (AD-29).

**Scope, derived.** The set of restore twins exercised here is checked
against every ``*Command`` Protocol under ``ports/`` that has a no-op in
``adapters/noop.py``, so a new restore command with a twin but no case here
fails.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.adapters.phase import SKIPPED_OUTCOME
from l3io.wp.backup.domain.archive_format import ArchiveView, ExtractResult, MemberRole
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.database import DatabaseLocation, EnsureOutcome, LoadResult
from l3io.wp.backup.domain.errors import AdminCredentialsRequiredError
from l3io.wp.backup.domain.journal import JournalHeader
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from l3io.wp.backup.domain.search_replace import (
    UNRESOLVED_URL,
    Replacement,
    SearchReplaceReport,
    UrlRelocation,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.port_roles import command_protocols

WHERE = DatabaseLocation("db.example", 3306, "wp")
SECRET = "never-logged-5c1e"
#: The twins never read the view; any value stands in for one.
VIEW = cast("ArchiveView", object())


def _target(path: Path) -> StorageTarget:
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "www"
    (root / "wp-content").mkdir(parents=True)
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    (root / "wp-config.php").write_text("<?php define('DB_NAME','wp');\n")
    (tmp_path / "journal").mkdir()
    return root


class RecordingSource:
    """A Dump source that fails the test if anything reads it."""

    def read(self, *_: object) -> bytes:
        pytest.fail("a no-op read its source")


def _header() -> JournalHeader:
    return JournalHeader(
        run_id="r-1",
        started_at="t",
        site_root=PurePosixPath("/srv/www"),
        restore_mode=RestoreMode.ALL,
        archive="a.tar.gz",
        snapshot_path=None,
        live_database_before="wp",
        staging_database=None,
        wp_config_path="/srv/www/wp-config.php",
        load_database="wp",
        cutover_keys=(),
        cutover_targets={},
        prior_constants={},
        phase_undo={},
        saved_config=None,
    )


#: Twin -> calls to make on it, each with the type it must return.
Call = Callable[[Any, Path], object]
CASES: dict[type, list[tuple[Call, type | None]]] = {
    noop.NoopDatabase: [
        (lambda a, s: a.ensure_database(WHERE), EnsureOutcome),
        (lambda a, s: a.load_dump(WHERE, RecordingSource()), LoadResult),
        (lambda a, s: a.load_dump(WHERE, s / "absent.sql"), LoadResult),
        (lambda a, s: a.revoke_grants(WHERE, user="wp", confirm="wp"), bool),
        (lambda a, s: a.drop_database(WHERE, confirm="wp"), bool),
    ],
    noop.NoopSiteFiles: [
        (
            lambda a, s: a.clean(s, keep=frozenset({PurePosixPath("wp-content")})),
            int,
        ),
        (
            lambda a, s: a.extract(
                _target(s.parent / "a.tar.gz"),
                s,
                VIEW,
                roles=frozenset({MemberRole.SITE_ROOT}),
                exclude=frozenset({PurePosixPath("wp-content/cache")}),
            ),
            ExtractResult,
        ),
    ],
    noop.NoopConfigWriter: [
        (
            lambda a, s: a.set_constants(
                s / "wp-config.php", {"DB_NAME": "x", "DB_PASSWORD": SECRET}
            ),
            ConfigWriteResult,
        ),
        (
            lambda a, s: a.save_copy(s / "wp-config.php", s.parent / "copy.php"),
            ConfigWriteResult,
        ),
        (
            lambda a, s: a.replace_file(
                s / "wp-config.php", "<?php\n", keep={"DB_HOST": "db"}
            ),
            ConfigWriteResult,
        ),
    ],
    noop.NoopSearchReplace: [
        (
            lambda a, s: a.relocate_content(
                s, UrlRelocation("https://new.example", None)
            ),
            SearchReplaceReport,
        ),
    ],
    noop.NoopRunJournal: [
        (lambda a, s: a.begin_run(_header()), None),
        (lambda a, s: a.phase_started(RestorePhase.CLEAN, str(s)), None),
        (lambda a, s: a.step_completed(RestorePhase.CLEAN, "remove"), None),
        (lambda a, s: a.phase_completed(RestorePhase.CLEAN), None),
        (lambda a, s: a.finish_run(), None),
        (lambda a, s: a.resolve("r-0", "operator"), None),
    ],
}


def _make(twin: type, log: noop.ActionLog) -> object:
    if twin is noop.NoopDatabase:
        return twin(log, administrator=True)
    return twin(log)


#: The backup twins, driven by the backup Dry Run's own tests.
BACKUP_TWINS = frozenset({"NoopArchiveStore", "NoopArchiveWriter"})


def twins(module: ModuleType, protocols: frozenset[type]) -> set[str]:
    """Classes defined in ``module`` implementing every member of a Protocol."""
    return {
        name
        for name, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__
        and any(
            all(hasattr(cls, m) for m in vars(p) if not m.startswith("_"))
            for p in protocols
        )
    }


def test_every_restore_twin_has_a_case() -> None:
    """Scope, derived: every twin of a ``*Command`` under ``ports/`` is driven."""
    found = twins(noop, command_protocols())
    assert found - BACKUP_TWINS == {t.__name__ for t in CASES}


def test_a_planted_twin_without_a_case_is_seen() -> None:
    """Scope attack: a new twin, named like nothing here, is still found."""
    planted = ModuleType("planted_noop")
    exec(  # noqa: S102 - a fixed planted module
        "class Shadow:\n"
        "    def relocate_content(self, site_root, urls): ...\n"
        "class Partial:\n"
        "    def begin_run(self, header): ...\n",
        planted.__dict__,
    )
    assert twins(planted, command_protocols()) == {"Shadow"}


@pytest.mark.parametrize("twin", list(CASES), ids=lambda t: t.__name__)
def test_each_twin_returns_its_type_touches_nothing_and_logs_skipped(
    twin: type, site: Path
) -> None:
    before = _tree(site.parent)
    log = noop.ActionLog()
    adapter = _make(twin, log)
    with capture_logs() as logs:
        for call, expected in CASES[twin]:
            result = call(adapter, site)
            if expected is None:
                assert result is None
            else:
                assert isinstance(result, expected)
            if hasattr(result, "performed"):
                assert result.performed is False
    assert _tree(site.parent) == before
    phases = [e for e in logs if e["event"] == "phase_completed"]
    assert len(phases) == len(CASES[twin])
    assert all(e["outcome"] == SKIPPED_OUTCOME and e["bytes"] == 0 for e in phases)
    assert [c.adapter for c in log.calls] == [twin.__name__] * len(CASES[twin])
    assert SECRET not in repr(logs) + repr(log.calls)


def test_the_config_twin_records_keys_never_values(site: Path) -> None:
    with capture_logs() as logs:
        noop.NoopConfigWriter(noop.ActionLog()).set_constants(
            site / "wp-config.php", {"DB_PASSWORD": SECRET, "DB_NAME": "x"}
        )
    (record,) = [e for e in logs if e["event"] == "phase_completed"]
    assert record["keys"] == ["DB_NAME", "DB_PASSWORD"]
    assert SECRET not in repr(record)


def test_the_site_files_twin_records_keep_and_exclude(site: Path) -> None:
    files = noop.NoopSiteFiles(noop.ActionLog())
    with capture_logs() as logs:
        files.clean(site, keep=frozenset({PurePosixPath("wp-content")}))
    (record,) = [e for e in logs if e["event"] == "phase_completed"]
    assert record["keep"] == ["wp-content"]


def test_the_twin_refuses_a_nested_keep_like_the_real_one(site: Path) -> None:
    """BL-E002-088: real adapter and no-op alike."""
    files = noop.NoopSiteFiles(noop.ActionLog())
    with pytest.raises(ValueError, match="top-level"):
        files.clean(site, keep=frozenset({PurePosixPath("wp-content/uploads")}))


@pytest.mark.parametrize("bad", ["/etc", "../outside", "a/../../b"])
def test_an_unsafe_keep_or_exclude_is_a_value_error(site: Path, bad: str) -> None:
    files = noop.NoopSiteFiles(noop.ActionLog())
    with pytest.raises(ValueError, match="keep"):
        files.clean(site, keep=frozenset({PurePosixPath(bad)}))
    with pytest.raises(ValueError, match="exclude"):
        files.extract(
            _target(site.parent / "a.tar.gz"),
            site,
            VIEW,
            roles=frozenset({MemberRole.SITE_ROOT}),
            exclude=frozenset({PurePosixPath(bad)}),
        )


def test_the_search_replace_twin_reports_planned_pairs_unresolved(
    site: Path,
) -> None:
    report = noop.NoopSearchReplace(noop.ActionLog()).relocate_content(
        site, UrlRelocation("https://a.example", "https://b.example")
    )
    assert report.replacements is None
    assert report.tables is None
    assert report.remaining is None
    assert report.pairs == (
        Replacement(UNRESOLVED_URL, "https://a.example"),
        Replacement(UNRESOLVED_URL, "https://b.example"),
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.ensure_database(WHERE),
        lambda d: d.revoke_grants(WHERE, user="wp", confirm="wp"),
        lambda d: d.drop_database(WHERE, confirm="wp"),
    ],
    ids=["ensure_database", "revoke_grants", "drop_database"],
)
def test_the_database_twin_refuses_without_an_administrator(
    call: Callable[[noop.NoopDatabase], object],
) -> None:
    log = noop.ActionLog()
    with pytest.raises(AdminCredentialsRequiredError, match="--admin-username"):
        call(noop.NoopDatabase(log))
    assert log.calls == ()


def test_load_dump_needs_no_administrator() -> None:
    result = noop.NoopDatabase(noop.ActionLog()).load_dump(WHERE, RecordingSource())  # type: ignore[arg-type]
    assert result == LoadResult(bytes=0, duration_ms=0.0, performed=False)


# --- BL-E002-093 (AD-2): the twins refuse where the real adapters refuse ------


@pytest.mark.parametrize(
    "shape",
    ["no-site-markers", "home", "filesystem-root"],
)
def test_the_site_files_twin_refuses_an_unsafe_clean_target_like_the_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    from l3io.wp.backup.adapters.site_files import LocalSiteFiles
    from l3io.wp.backup.domain.errors import UnsafeCleanTargetError

    target = tmp_path / "not-a-site"
    target.mkdir()
    (target / "keep-me.txt").write_text("x", encoding="utf-8")
    if shape == "home":
        (target / "wp-config.php").write_text("<?php\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(target))
    elif shape == "filesystem-root":
        target = Path("/")
    before = sorted(p.name for p in target.iterdir())
    for adapter in (noop.NoopSiteFiles(noop.ActionLog()), LocalSiteFiles()):
        with pytest.raises(UnsafeCleanTargetError):
            adapter.clean(target, keep=frozenset())
    assert sorted(p.name for p in target.iterdir()) == before


def test_the_config_twin_refuses_an_unkeepable_constant_like_the_real_one(
    site: Path,
) -> None:
    pytest.importorskip("l3io.wp.config")
    from l3io.wp.backup.adapters.config_writer import WpConfigWriter
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.domain.errors import ConfigWriteFailedError

    config = site / "wp-config.php"
    before = config.read_bytes()
    for adapter in (
        noop.NoopConfigWriter(noop.ActionLog()),
        WpConfigWriter(SecretRegistry()),
    ):
        with pytest.raises(ConfigWriteFailedError) as caught:
            adapter.replace_file(config, "<?php\n", keep={"DB_HOST": None})
        assert caught.value.key == "DB_HOST"
    assert config.read_bytes() == before


# --- BL-E002-107 (SD-02-1): the executor, not the no-op, names the phase -------


@pytest.mark.parametrize("twin", list(CASES), ids=lambda t: t.__name__)
@pytest.mark.parametrize("open_phase", [p.value for p in RestorePhase])
def test_a_twin_called_inside_any_phase_joins_it_and_emits_none_of_its_own(
    twin: type, open_phase: str, site: Path
) -> None:
    from l3io.wp.backup.adapters.phase import phase

    adapter = _make(twin, noop.ActionLog())
    with capture_logs() as logs, phase(open_phase):
        for call, _expected in CASES[twin]:
            call(adapter, site)
    records = [e for e in logs if e["event"] == "phase_completed"]
    assert [r["phase"] for r in records] == [open_phase]
    assert records[0]["outcome"] == SKIPPED_OUTCOME


def test_a_dry_run_cutover_is_recorded_inside_load_dump(site: Path) -> None:
    """The reproduction: ``set_constants`` inside ``load-dump`` (AD-17)."""
    from l3io.wp.backup.adapters.phase import phase

    with capture_logs() as logs, phase(RestorePhase.LOAD_DUMP.value):
        noop.NoopConfigWriter(noop.ActionLog()).set_constants(
            site / "wp-config.php", {"DB_NAME": "x"}
        )
    phases = [e["phase"] for e in logs if e["event"] == "phase_completed"]
    assert phases == [RestorePhase.LOAD_DUMP.value]
