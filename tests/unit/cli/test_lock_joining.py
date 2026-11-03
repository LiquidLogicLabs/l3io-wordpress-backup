"""AD-16/AD-19 — a joined entry point locks only a Site its Run does not hold.

With a recording ``SiteLock``: a driver joining a Run whose lock is keyed on
the same canonical Site Root acquires and releases nothing — including
``drive_backup`` and ``backup(..., ctx=ctx)`` inside that Run, and a different
spelling of the same Site; joined for a different Site, or a Run holding no
lock, it acquires that Site's lock under the Run's ID and releases it itself.
The real ``FcntlSiteLock`` is driven once too: a CLI-shaped nested Run on the
same Site does not refuse itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, join_or_open, open_run
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from tests.unit.cli.conftest import RecordingSiteLock

pytestmark = pytest.mark.usefixtures("fake_database")


@pytest.fixture
def recorder() -> RecordingSiteLock:
    return RecordingSiteLock()


@pytest.fixture
def run(site: Path, recorder: RecordingSiteLock, workdir: Path) -> Iterator[RunContext]:
    """A Run opened for ``site``, its adapters bound."""
    with open_run(
        operation="backup", site_root=site.resolve(), site_lock=recorder
    ) as ctx:
        ctx.bind_adapters(
            real_adapters(
                wordpress_source=object(),  # type: ignore[arg-type]
                secrets=ctx.secrets,
                tracer=ctx.tracer,
            )
        )
        yield ctx


def _request(site: Path, final: Path) -> backup_cli.BackupRequest:
    return backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.FILES,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )


def test_the_outer_run_holds_the_lock(
    run: RunContext, recorder: RecordingSiteLock, site: Path
) -> None:
    assert recorder.events == [("lock.acquire", site.resolve(), run.run_id)]
    assert run.site_lock is not None
    assert run.site_lock.site_root == site.resolve()


def test_joining_for_the_same_site_acquires_nothing(
    run: RunContext, recorder: RecordingSiteLock, site: Path
) -> None:
    before = list(recorder.events)
    with join_or_open(
        run, operation="backup", site_root=site.resolve(), site_lock=recorder
    ) as handle:
        assert handle.ctx is run
    assert recorder.events == before


def test_drive_backup_joined_for_the_same_site_acquires_nothing(
    run: RunContext, recorder: RecordingSiteLock, site: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(site, target_is_directory=True)
    before = list(recorder.events)
    with capture_logs():
        result = backup_cli.drive_backup(
            _request(alias, tmp_path / "a.tar.gz"), ctx=run, site_lock=recorder
        )
    assert result.performed
    assert recorder.events == before


def test_backup_inside_the_run_acquires_nothing(
    run: RunContext, recorder: RecordingSiteLock, site: Path, tmp_path: Path
) -> None:
    before = list(recorder.events)
    config = locate_wp_config(site.resolve(), stated=None)
    descriptor, _ = site_descriptor_from_wp_config(
        site.resolve(), config_file=config.path
    )
    with capture_logs():
        backup_cli.backup(
            descriptor,
            BackupScope.FILES,
            backup_cli.storage_target_from_cli(str(tmp_path / "b.tar.gz")),
            ctx=run,
            config=config,
            cipher=None,
            exclusion=INCLUDE_EVERYTHING,
            origin=None,
        )
    assert recorder.events == before


def test_joining_for_a_different_site_acquires_and_releases_it(
    run: RunContext, recorder: RecordingSiteLock, tmp_path: Path
) -> None:
    other = (tmp_path / "other").resolve()
    other.mkdir()
    before = list(recorder.events)
    with join_or_open(run, operation="backup", site_root=other, site_lock=recorder):
        assert recorder.events[len(before) :] == [("lock.acquire", other, run.run_id)]
    assert recorder.events[len(before) :] == [
        ("lock.acquire", other, run.run_id),
        ("lock.release", other),
    ]


def test_joining_a_run_that_holds_no_lock_acquires_and_releases(
    recorder: RecordingSiteLock, site: Path, workdir: Path
) -> None:
    canonical = site.resolve()
    with open_run(operation="verify") as ctx:
        assert ctx.site_lock is None
        with join_or_open(
            ctx, operation="backup", site_root=canonical, site_lock=recorder
        ):
            pass
    assert recorder.events == [
        ("lock.acquire", canonical, ctx.run_id),
        ("lock.release", canonical),
    ]


def test_the_opened_run_releases_on_leaving(
    recorder: RecordingSiteLock, site: Path, workdir: Path
) -> None:
    canonical = site.resolve()
    with join_or_open(
        None, operation="backup", site_root=canonical, site_lock=recorder
    ) as handle:
        assert handle.opened
    assert recorder.events[-1] == ("lock.release", canonical)


@pytest.mark.parametrize("given", ["site_root", "site_lock"])
def test_one_of_the_pair_alone_is_refused(
    recorder: RecordingSiteLock, site: Path, workdir: Path, given: str
) -> None:
    kwargs: dict[str, object] = (
        {"site_root": site.resolve()}
        if given == "site_root"
        else {"site_lock": recorder}
    )
    with (
        pytest.raises(UsageError, match="both site_root and site_lock"),
        join_or_open(None, operation="backup", **kwargs),  # type: ignore[arg-type]
    ):
        pass
    assert recorder.events == []


def test_the_real_lock_lets_its_own_run_rejoin_the_same_site(
    site: Path, tmp_path: Path, workdir: Path
) -> None:
    lock = FcntlSiteLock(tmp_path / "locks")
    canonical = site.resolve()
    with open_run(operation="backup", site_root=canonical, site_lock=lock) as ctx:
        ctx.bind_adapters(
            real_adapters(
                wordpress_source=object(),  # type: ignore[arg-type]
                secrets=ctx.secrets,
                tracer=ctx.tracer,
            )
        )
        with capture_logs():
            result = backup_cli.drive_backup(
                _request(site, tmp_path / "c.tar.gz"), ctx=ctx, site_lock=lock
            )
        assert result.performed
    # Released at the end of the outer Run: another Run may take it.
    with open_run(operation="backup", site_root=canonical, site_lock=lock):
        pass
