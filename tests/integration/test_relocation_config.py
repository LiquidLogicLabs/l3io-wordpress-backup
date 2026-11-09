"""E007-S01-001 (FR-11, AD-17) — a relocating restore over the CI database.

The plan is built through the real extension (``adapters/wpcli.py``'s
``resolve``) and the built ``build_restore_plan``, then run by
``execute_restore`` over the real adapters (``tests/integration/live_restore``).
One case per ``--new-*`` flag of the restore subparser (derived from the
parser; ``--new-db-user`` with each password source, ``--new-db-password`` or
``--new-db-password-file``, as they must be), plus all of them at once. The
password is resolved by the CLI's own chain (``cli/credentials.new_db_password``);
``--new-db-password-file`` is a ``0600`` file holding the sentinel password:

* every supplied value reaches ``wp-config.php`` (seeded in WordPress's
  ``define( 'KEY', 'value' );`` spacing, every key defined) and every other
  byte is the original's, by literal substitution of the changed value spans;
  ``php -l`` accepts the result;
* ``DB_NAME`` ends as ``--new-db-name`` (else the staging name), and that
  database holds the Archive's tables;
* the relocated password is in ``wp-config.php`` and in no journal file.

Then: a read-back mismatch (``l3io.wp.config``'s ``verify_write`` patched)
leaves the file byte-identical, journals ``REWRITE_CONFIG`` started and not
completed, and loads nothing; a non-empty ``--new-db-name`` database is
``REFUSED`` (under ``--force``, by the database checks) with nothing changed;
and a DB-only relocation runs end to end through ``wpbackup2 restore``.

BL-E007-008 (E008-S01-001): a ``wp-cli.yml`` in the per-Site state directory's
parent makes a URL-relocating ``wpbackup2 restore`` exit 3
(``WpCliConfigFoundError``) before any effect -- the extension's ``resolve``
checks the directory WP-CLI would run from -- with the Site Root, its config
and every database untouched.

Not here: a URL relocation through ``wpbackup2 restore`` (the root's WP-CLI
preflight is E007-S01-004's; the end-to-end case is E008-S01-001's), and a
non-empty load database refused at planning without ``--force`` (the live
database is populated, so only a waiver that also waives it gets that far;
``check_target`` is unit-tested in ``tests/unit/core/test_target_guard.py``).

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``) and ``php``; skips
without the database.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.config import WpConfigString
from l3io.wp.config.adapters import config_file as library_file

from l3io.wp.backup.adapters.database import site_descriptor_from_wp_config
from l3io.wp.backup.adapters.journal import journal_directory
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_extension import restore_extension
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import verify_archive_view
from l3io.wp.backup.cli.credentials import new_db_password
from l3io.wp.backup.cli.parser import RESTORE_COMMAND, build_parser
from l3io.wp.backup.cli.restore_request import RelocationFlags
from l3io.wp.backup.core.restore import RestoreRelocation, staging_name
from l3io.wp.backup.domain.errors import BackupError, ConfigWriteFailedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import (
    CliSite,
    admin_account,
    authorizing_flags,
    cli_site,
    run_cli,
    tree,
)
from tests.integration.live_restore import (
    LiveSite,
    build_archive,
    host,
    journal_for,
    live_site,
    port,
    read_keys,
    restore,
    root_connection,
    sha,
    spec_for,
)

__all__ = ["admin_account", "cli_site", "live_site"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

#: Each ``--new-*`` flag and the ``wp-config.php`` constant it must change. A
#: parser flag missing here fails ``test_the_table_covers_every_new_flag``.
FLAG_KEY = {
    "--new-site-url": "WP_SITEURL",
    "--new-site-home-url": "WP_HOME",
    "--new-db-host": "DB_HOST",
    "--new-db-name": "DB_NAME",
    "--new-db-user": "DB_USER",
    "--new-db-password": "DB_PASSWORD",
    "--new-db-password-file": "DB_PASSWORD",
}
#: ``--new-*`` flag → the ``RelocationFlags`` attribute it fills.
ATTRIBUTE = {
    "--new-site-url": "new_site_url",
    "--new-site-home-url": "new_home_url",
    "--new-db-host": "new_db_host",
    "--new-db-name": "new_db_name",
    "--new-db-user": "new_db_user",
    "--new-db-password": "new_db_password",
    "--new-db-password-file": "new_db_password",
}
#: The relocated password's sources; each is given with ``--new-db-user``.
PASSWORD_FLAGS = frozenset({"--new-db-password", "--new-db-password-file"})
USER_FLAG = "--new-db-user"


def _new_flags() -> list[str]:
    for action in build_parser()._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            restore_parser = action.choices[RESTORE_COMMAND]
            return sorted(
                option
                for a in restore_parser._actions  # noqa: SLF001
                for option in a.option_strings
                if option.startswith("--new-")
            )
    msg = "no subcommands"
    raise AssertionError(msg)


def _groups() -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    for flag in _new_flags():
        if flag == USER_FLAG:
            continue  # with each password source, below
        group = tuple(sorted({flag, USER_FLAG})) if flag in PASSWORD_FLAGS else (flag,)
        if group not in groups:
            groups.append(group)
    groups.append(tuple(_new_flags()))
    return groups


def test_the_table_covers_every_new_flag() -> None:
    assert _new_flags(), "the restore subparser registers no --new-* flag"
    assert set(_new_flags()) == set(FLAG_KEY) == set(ATTRIBUTE)


def _values(site: LiveSite, sentinel: CredentialSentinel) -> dict[str, str]:
    return {
        "--new-site-url": "https://staging.e007.example",
        "--new-site-home-url": "https://www.staging.e007.example/blog",
        "--new-db-host": f"{host()}:{port()}",
        "--new-db-name": f"{site.database}_rel",
        "--new-db-user": f"wpn_{uuid.uuid4().hex[:10]}",
        "--new-db-password": sentinel.relocated_password,
        "--new-db-password-file": str(_password_file(site, sentinel)),
    }


def _password_file(site: LiveSite, sentinel: CredentialSentinel) -> Path:
    """The sentinel password in a ``0600`` file, as an operator would keep it."""
    path = site.work / f"new-db-password-{uuid.uuid4().hex[:8]}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{sentinel.relocated_password}\n")
    assert path.stat().st_mode & 0o777 == 0o600
    return path


def _expected(values: dict[str, str], sentinel: CredentialSentinel) -> dict[str, str]:
    """Each flag's ``wp-config.php`` value: the argument, or the file's password."""
    return {**values, "--new-db-password-file": sentinel.relocated_password}


def _relocation_flags(given: dict[str, str]) -> RelocationFlags:
    """``given`` as ``run_restore`` translates it; the password by its chain."""
    file = given.get("--new-db-password-file")
    password = new_db_password(
        explicit=given.get("--new-db-password"),
        file=None if file is None else Path(file),
        environ={},
        secrets=SecretRegistry(),
        user_given=USER_FLAG in given,
    )
    return RelocationFlags(
        **{
            attribute: given.get(flag)
            for flag, attribute in ATTRIBUTE.items()
            if flag not in PASSWORD_FLAGS
        },
        new_db_password=password,
        wp_config=None,
    )


def _resolve(
    site: LiveSite, flags: RelocationFlags, mode: RestoreMode, archive: Path
) -> RestoreRelocation:
    descriptor, _ = site_descriptor_from_wp_config(
        site.site_root, config_file=site.config, environ={}, secrets=SecretRegistry()
    )
    view, _ = verify_archive_view(archive)
    assert view is not None
    relocation = restore_extension(flags, secrets=SecretRegistry()).resolve(
        site_root=site.site_root,
        site=descriptor,
        view=view,
        files=LocalSiteFiles(),
        mode=mode,
    )
    # The content rewrite (SEARCH_REPLACE, E007-S01-003) needs a WordPress
    # install and ``wp``; this Site Root is neither, and the rewrite is
    # tests/integration/test_search_replace.py's. Here: the config only.
    return dataclasses.replace(
        relocation,
        search_replace=None,
        undo=tuple(
            (phase, text)
            for phase, text in relocation.undo
            if phase is not RestorePhase.SEARCH_REPLACE
        ),
    )


def _define_line(key: str, value: str) -> str:
    """WordPress's own spelling: ``define( 'KEY', 'value' );``."""
    return f"define( '{key}', '{value}' );"


def _prior(text: str, key: str) -> str:
    """``key``'s value in ``text``, read with ``l3io-wordpress-config``."""
    value = WpConfigString(text).get(key)
    assert isinstance(value, str), key
    return value


def _write_wordpress_config(site: LiveSite) -> None:
    """The Site's config in WordPress's spacing, the URL constants defined.

    Every key the restore may change is already defined, so each write is an
    in-place value-span change; the comment and ``require_once`` lines are
    bytes this tool does not manage and must keep.
    """
    lines = [
        "<?php",
        "/**",
        " * The base configuration for WordPress (a test fixture).",
        " */",
        "",
        "// ** Database settings ** //",
        _define_line("DB_NAME", site.database),
        _define_line("DB_USER", site.user),
        _define_line("DB_PASSWORD", site.password),
        _define_line("DB_HOST", host()),
        _define_line("DB_CHARSET", "utf8mb4"),
        "",
        _define_line("WP_SITEURL", "https://old-site.e007.example"),
        _define_line("WP_HOME", "https://old-home.e007.example"),
        "",
        "$table_prefix = 'wp_';",
        "",
        "/* That's all, stop editing! Happy publishing. */",
        "if ( ! defined( 'ABSPATH' ) ) {",
        "\tdefine( 'ABSPATH', __DIR__ . '/' );",
        "}",
        "require_once ABSPATH . 'wp-settings.php';",
        "",
    ]
    site.config.write_text("\n".join(lines), encoding="utf-8")


def _php_lint(path: Path) -> None:
    php = shutil.which("php")
    assert php is not None, "php is not on PATH (CI installs php-cli)"
    lint = subprocess.run(  # noqa: S603 - a fixed argv
        [php, "-l", str(path)], capture_output=True, text=True, check=False
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr


def _tables(database: str) -> set[str]:
    with root_connection(database).cursor() as cursor:
        cursor.execute("SHOW TABLES")
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.parametrize("group", _groups(), ids="+".join)
def test_each_relocation_flag_reaches_wp_config(
    live_site: LiveSite, credential_sentinel: CredentialSentinel, group: tuple[str, ...]
) -> None:
    site = live_site
    values = _values(site, credential_sentinel)
    expected_values = _expected(values, credential_sentinel)
    given = {flag: values[flag] for flag in group}
    if USER_FLAG in given:
        site.extra_users.append(given[USER_FLAG])
    flags = _relocation_flags(given)
    _write_wordpress_config(site)
    archive = build_archive(site.work, config=None)
    mode = RestoreMode.DATABASE
    relocation = _resolve(site, flags, mode, archive)
    before = site.config.read_text(encoding="utf-8")
    spec = spec_for(
        site,
        archive,
        mode,
        relocation=relocation,
        relocated_user=given.get(USER_FLAG),
        relocated_password=(
            credential_sentinel.relocated_password
            if PASSWORD_FLAGS & given.keys()
            else None
        ),
    )
    restore(spec)

    load = given.get("--new-db-name") or staging_name(site.database, spec.run_id)
    # Literal value-span substitution on the original text, not a re-render
    # by the library that wrote the file: only these spans may differ.
    changed = {"DB_NAME": load}
    changed.update({FLAG_KEY[flag]: expected_values[flag] for flag in given})
    expected = before
    for key, value in changed.items():
        line = _define_line(key, _prior(before, key))
        assert expected.count(line) == 1, key
        expected = expected.replace(line, _define_line(key, value))
    after = site.config.read_text(encoding="utf-8")
    assert after == expected
    for flag in given:
        landed = WpConfigString(after).get(FLAG_KEY[flag])
        assert sha(landed) == sha(expected_values[flag]), flag
    _php_lint(site.config)
    assert "wp_restored" in _tables(load)

    directory = journal_for(site.site_root).directory
    for path in sorted(directory.rglob("*.json")):
        credential_sentinel.add(f"journal {path.name}", path.read_text("utf-8"))
    credential_sentinel.assert_clean()


def test_a_read_back_mismatch_changes_nothing_and_loads_nothing(
    live_site: LiveSite,
    credential_sentinel: CredentialSentinel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = live_site
    values = _values(site, credential_sentinel)
    flags = RelocationFlags(
        new_site_url=values["--new-site-url"],
        new_home_url=None,
        new_db_host=None,
        new_db_name=values["--new-db-name"],
        new_db_user=None,
        new_db_password=None,
        wp_config=None,
    )
    archive = build_archive(site.work, config=None)
    relocation = _resolve(site, flags, RestoreMode.DATABASE, archive)
    before = site.config.read_bytes()
    databases = site.databases()
    monkeypatch.setattr(library_file, "verify_write", lambda **_: ["forced"])
    spec = spec_for(site, archive, RestoreMode.DATABASE, relocation=relocation)
    with pytest.raises(ConfigWriteFailedError) as caught:
        restore(spec)
    assert caught.value.exit_code is ExitCode.FAILED
    assert site.config.read_bytes() == before
    assert site.databases() == databases, "LOAD_DUMP ran after a failed rewrite"
    record = journal_for(site.site_root).incomplete(PurePosixPath(site.site_root))
    assert record is not None, "the failed Run left no incomplete journal"
    events = [(e.event, e.phase) for e in record.entries]
    rewrite = RestorePhase.REWRITE_CONFIG
    assert ("phase_started", rewrite) in events
    assert ("phase_completed", rewrite) not in events
    assert ("phase_started", RestorePhase.LOAD_DUMP) not in events


def test_a_non_empty_new_database_is_refused_and_nothing_changes(
    live_site: LiveSite, credential_sentinel: CredentialSentinel
) -> None:
    """Under ``--force`` (the spec's waivers), the database checks still refuse.

    ``check_staged`` at ``PREFLIGHT`` reports the database not empty, so the
    refusal comes before any destructive phase (``LOAD_DUMP`` is the fallback
    only when the account cannot see the database).
    """
    site = live_site
    occupied = f"{site.database}_full"
    with root_connection().cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{occupied}`")
        cursor.execute(f"CREATE TABLE `{occupied}`.`keep_me` (`id` int PRIMARY KEY)")
        cursor.execute(f"INSERT INTO `{occupied}`.`keep_me` VALUES (7)")  # noqa: S608
    flags = RelocationFlags(
        new_site_url=None,
        new_home_url=None,
        new_db_host=None,
        new_db_name=occupied,
        new_db_user=None,
        new_db_password=None,
        wp_config=None,
    )
    archive = build_archive(site.work, config=None)
    relocation = _resolve(site, flags, RestoreMode.DATABASE, archive)
    before = site.config.read_bytes()
    checksums = site.checksums(occupied)
    spec = spec_for(site, archive, RestoreMode.DATABASE, relocation=relocation)
    with pytest.raises(BackupError) as caught:
        restore(spec)
    assert caught.value.exit_code is ExitCode.REFUSED, caught.value
    assert site.config.read_bytes() == before
    assert site.checksums(occupied) == checksums
    assert _tables(occupied) == {"keep_me"}
    record = journal_for(site.site_root).incomplete(PurePosixPath(site.site_root))
    started = [] if record is None else [e.phase for e in record.entries]
    assert RestorePhase.LOAD_DUMP not in started, started


def test_a_db_only_relocation_runs_through_the_cli(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = cli_site
    new_name = f"{site.site.database}_cli"
    new_user = f"wpn_{uuid.uuid4().hex[:10]}"
    site.site.extra_users.append(new_user)
    password = credential_sentinel.relocated_password
    _write_wordpress_config(site.site)
    before = site.site.config.read_text(encoding="utf-8")
    ran = run_cli(
        site.argv(
            site.archive(),
            *authorizing_flags(RestoreMode.DATABASE),
            "--new-db-host",
            f"{host()}:{port()}",
            "--new-db-name",
            new_name,
            "--new-db-user",
            new_user,
            "--new-db-password",
            password,
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.OK), ran.err[-3000:]
    keys = read_keys(site.site.config)
    assert keys["DB_NAME"] == new_name
    assert keys["DB_USER"] == new_user
    assert keys["DB_HOST"] == f"{host()}:{port()}"
    assert sha(keys["DB_PASSWORD"]) == sha(password)
    expected = before
    for key, value in keys.items():
        line = _define_line(key, _prior(before, key))
        expected = expected.replace(line, _define_line(key, str(value)))
    assert site.site.config.read_text(encoding="utf-8") == expected
    _php_lint(site.site.config)
    assert "wp_restored" in _tables(new_name)
    (planned,) = ran.events("relocation_planned")
    assert planned["db_location"]["name"] == new_name
    credential_sentinel.add("stdout", ran.out)
    credential_sentinel.add("stderr", ran.err)
    credential_sentinel.assert_clean()


@pytest.mark.parametrize("name", ["wp-cli.yml", "wp-cli.local.yml"])
def test_a_wp_cli_config_above_the_state_directory_refuses_a_url_relocation(
    cli_site: CliSite, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    site = cli_site
    state = journal_directory(site.root.resolve())
    state.parent.mkdir(parents=True, exist_ok=True)
    planted = state.parent / name
    planted.write_text("require:\n  - e008-planted.php\n", encoding="utf-8")
    archive = site.archive()
    before = (
        tree(site.root),
        site.site.config.read_bytes(),
        site.site.checksums(),
        site.site.databases(),
    )
    ran = run_cli(
        site.argv(
            archive,
            *authorizing_flags(RestoreMode.ALL),
            "--new-site-url",
            "https://staging.e008.example",
        ),
        capsys,
    )
    assert ran.code == int(ExitCode.REFUSED) == 3, ran.err[-3000:]
    (failed,) = ran.events("command_failed")
    assert failed["error"] == "WpCliConfigFoundError"
    assert str(planted) in str(failed["message"])
    assert (
        tree(site.root),
        site.site.config.read_bytes(),
        site.site.checksums(),
        site.site.databases(),
    ) == before
    assert not ran.events("phase_started"), "a phase started before the refusal"
