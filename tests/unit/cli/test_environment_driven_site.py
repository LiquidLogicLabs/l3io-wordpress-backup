"""Owner decision 47 — the official image's ``getenv_docker()`` site, end to end.

Backup and doctor resolve the Site's ``DB_*`` from the Run's environment, and
log the **resolved** host (owner decision 54). A database restore loads the
database the environment names and writes no ``DB_*`` (owner decision 49,
ADR-0027, Accepted); only a relocation that would rewrite one refuses early,
a Dry Run included. A files-only restore proceeds, and with
``--overwrite-wp-config`` carries the definitions into the Archive's file as
written, refusing early an Archive's file that does not define
``getenv_docker()`` (owner decision 48).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("l3io.wp.config")

from l3io.wp.config import EnvLookup, WpConfigFile

from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.doctor import drive_doctor
from l3io.wp.backup.domain.errors import (
    EnvironmentDrivenTargetError,
    UndefinedConfigFunctionError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import ConfigPolicy, RestoreMode, restores_database
from tests.restore_harness import write_v1
from tests.restore_root_rig import Rig
from tests.unit.cli.conftest import FakeDatabase

DB_PASSWORD = "env-sidecar-password-3b7e1f"

DOCKER_DEFINES = (
    "<?php\n"
    "define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );\n"
    "define( 'DB_USER', getenv_docker('WORDPRESS_DB_USER', 'example username') );\n"
    "define( 'DB_PASSWORD', "
    "getenv_docker('WORDPRESS_DB_PASSWORD', 'example password') );\n"
    "define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );\n"
    "$table_prefix = getenv_docker('WORDPRESS_TABLE_PREFIX', 'wp_');\n"
)
SITE_ENV = {
    "WORDPRESS_DB_HOST": "db.example",
    "WORDPRESS_DB_NAME": "wpdb_site",
    "WORDPRESS_DB_USER": "wpu_site",
    "WORDPRESS_DB_PASSWORD": DB_PASSWORD,
}


@pytest.fixture
def docker_site(site: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (site / "wp-config.php").write_text(DOCKER_DEFINES, encoding="utf-8")
    for name, value in SITE_ENV.items():
        monkeypatch.setenv(name, value)
    return site


# --------------------------------------------------------------------------
# Backup, doctor
# --------------------------------------------------------------------------


def test_the_resolved_host_is_logged_and_the_site_backed_up(
    docker_site: Path,
    tmp_path: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Owner decision 54: the host is the resolved one, logged, never confirmed."""
    final = tmp_path / "out.tar.gz"
    argv = ["--log-level", "DEBUG", "--log-format", "json", "backup"]
    code = main([*argv, "--wp-dir", str(docker_site), "--archive", str(final)])
    out = capsys.readouterr()
    assert code == 0, out.err
    [location] = fake_database.checks
    assert (location.host, location.name) == ("db.example", "wpdb_site")
    assert len(fake_database.calls) == 1
    assert final.exists()
    assert '"event": "database_host"' in out.err
    assert '"db_host": "db.example"' in out.err
    assert "getenv_docker('WORDPRESS_DB_HOST'" not in out.err
    assert DB_PASSWORD not in out.err + out.out


def test_doctor_reads_the_site_from_the_environment(
    docker_site: Path, fake_database: FakeDatabase
) -> None:
    drive_doctor(site_root=docker_site, target=None, environ=SITE_ENV)
    [location] = fake_database.checks
    assert (location.host, location.name) == ("db.example", "wpdb_site")


# --------------------------------------------------------------------------
# Restore: refused where it would rewrite the definitions (ADR-0027)
# --------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    built = Rig(tmp_path, monkeypatch)
    built.config.write_text(DOCKER_DEFINES, encoding="utf-8")
    return built


DATABASE_MODES = [m for m in RestoreMode if restores_database(m)]
FILE_MODES = [m for m in RestoreMode if not restores_database(m)]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", DATABASE_MODES)
def test_a_database_restore_of_an_environment_driven_target_proceeds(
    rig: Rig, mode: RestoreMode, dry_run: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 49: no ``DB_NAME`` cutover, so nothing to refuse."""
    before = rig.config.read_text(encoding="utf-8")
    rig.drive(mode=mode, dry_run=dry_run)
    assert rig.executions
    assert rig.inputs.header.cutover_keys == ()
    assert rig.config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_relocation_of_an_environment_driven_key_is_refused(
    rig: Rig, dry_run: bool
) -> None:
    import dataclasses

    from l3io.wp.backup.cli.restore_request import RelocationFlags

    flags = dataclasses.replace(RelocationFlags.none(), new_db_name="other_db")
    with pytest.raises(EnvironmentDrivenTargetError) as caught:
        rig.drive(mode=RestoreMode.DATABASE, dry_run=dry_run, relocation=flags)
    assert caught.value.keys == ("DB_NAME",)
    assert "--new-db-*" in caught.value.operation
    assert type(caught.value).exit_code is ExitCode.REFUSED
    assert "real_adapters" not in rig.events
    assert rig.executions == []


#: The official image's helper, which a carried ``getenv_docker()`` needs.
DOCKER_HELPER = (
    "if (!function_exists('getenv_docker')) {\n"
    "\tfunction getenv_docker($env, $default) {\n"
    "\t\tif (($val = getenv($env)) !== false) { return $val; }\n"
    "\t\treturn $default;\n"
    "\t}\n"
    "}\n"
)


def _docker_archive(rig: Rig, text: str) -> None:
    """Replace the rig's Archive with one whose ``wp-config.php`` is ``text``."""
    work = rig.work / "docker-archive"
    work.mkdir()
    rig.archive_path = write_v1(work, config_text=text)


@pytest.mark.parametrize("dry_run", [False, True])
def test_overwrite_wp_config_from_an_archive_without_getenv_docker_is_refused(
    rig: Rig, dry_run: bool
) -> None:
    """Owner decision 48: the carried lookups would call an undefined function."""
    before = rig.config.read_bytes()
    with pytest.raises(UndefinedConfigFunctionError) as caught:
        rig.drive(
            mode=RestoreMode.FILES,
            config_policy=ConfigPolicy.OVERWRITE,
            dry_run=dry_run,
        )
    assert caught.value.function == "getenv_docker"
    assert set(caught.value.keys) == {"DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"}
    assert type(caught.value).exit_code is ExitCode.REFUSED
    assert "real_adapters" not in rig.events
    assert rig.executions == []
    assert rig.config.read_bytes() == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_overwrite_wp_config_carries_the_lookups_into_the_archives_config(
    rig: Rig, dry_run: bool
) -> None:
    """BL-E008-032: the target's lookups stay lookups, not fixed strings."""
    archived = (
        "<?php\n"
        + DOCKER_HELPER
        + "define( 'DB_NAME', 'source_db' );\n"
        + "define( 'DB_USER', 'source_user' );\n"
        + "define( 'DB_PASSWORD', 'source-password' );\n"
        + "define( 'DB_HOST', 'source-host' );\n"
        + "$table_prefix = 'wp_';\n"
        + "// the archive's own line\n"
    )
    _docker_archive(rig, archived)
    rig.real_executor()
    before = rig.config.read_bytes()
    rig.drive(
        mode=RestoreMode.FILES,
        config_policy=ConfigPolicy.OVERWRITE,
        dry_run=dry_run,
    )
    assert len(rig.executions) == 1
    if dry_run:
        assert rig.config.read_bytes() == before
        return
    landed = rig.config.read_text(encoding="utf-8")
    assert "// the archive's own line" in landed
    assert "source-password" not in landed
    config = WpConfigFile(rig.config)
    for key, variable in (
        ("DB_HOST", "WORDPRESS_DB_HOST"),
        ("DB_NAME", "WORDPRESS_DB_NAME"),
        ("DB_USER", "WORDPRESS_DB_USER"),
        ("DB_PASSWORD", "WORDPRESS_DB_PASSWORD"),
    ):
        value = config.get_value(key)
        assert isinstance(value, EnvLookup), key
        assert value.name == variable
        assert value.function == "getenv_docker"


@pytest.mark.parametrize("mode", FILE_MODES)
def test_a_files_restore_keeping_the_target_config_proceeds(
    rig: Rig, mode: RestoreMode
) -> None:
    rig.drive(mode=mode)
    assert len(rig.executions) == 1
    assert rig.inputs.site.db_name == "wordpress"  # the default: nothing set


def test_the_cli_exits_refused_for_a_relocation_of_an_environment_key(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["restore", "--wp-dir", str(rig.site_root), "--archive"]
    code = main(
        [
            "--whatIf",
            *argv,
            str(rig.archive_path),
            "--mode",
            "all",
            "--new-db-name",
            "other_db",
        ]
    )
    err = capsys.readouterr().err
    assert code == ExitCode.REFUSED
    assert "EnvironmentDrivenTargetError" in err
    assert "0027" in err


def test_the_cli_exits_refused_for_an_overwrite_without_getenv_docker(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["restore", "--wp-dir", str(rig.site_root), "--archive"]
    code = main(
        [
            "--whatIf",
            *argv,
            str(rig.archive_path),
            "--mode",
            "files",
            "--overwrite-wp-config",
            "--force",
        ]
    )
    err = capsys.readouterr().err
    assert code == ExitCode.REFUSED
    assert "UndefinedConfigFunctionError" in err
    assert "Call to undefined function getenv_docker()" in err
    assert DB_PASSWORD not in err
