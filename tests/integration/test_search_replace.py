"""E007-S01-003 (FR-12, ADR-0005) — content URLs rewritten by the real WP-CLI.

A real WordPress (core downloaded once per session by ``wp core download``,
pinned below) is installed into a uniquely named throwaway database at the old
URL, seeded with a serialized option, a widget option, serialized postmeta, a
``post_content`` URL, a plugin table ``wp_plugin_x`` and a ``guid``, and then
relocated by ``WpCliSearchReplace`` to a URL of a different length. Asserted:

* **oracle** — an independent PyMySQL count of cells holding the old URL, over
  every text column of every ``wp_``-prefixed table except ``guid``, is 0
  afterwards, and the reported total equals its count beforehand;
* **serialization intact** — ``wp option get --format=json`` decodes the
  serialized option and the widget to the expected structures carrying the new
  URL (PHP unserialized them: the length prefixes are right);
* the ``guid`` is unchanged;
* ``option list`` read the **stored** URLs with ``WP_HOME`` defined to the new
  one (``option get`` would have returned the constant);
* a ``wp-cli.yml`` with ``require: marker.php`` in the Site Root and in its
  parent was never loaded; one in the state directory's parent is refused;
* a restored mu-plugin **does** run (the documented residual, observed);
* the Dry Run twin leaves the oracle count unchanged and reports the pair.

Route (a decision, recorded in the story's completion notes): the adapter is
driven directly on an installed site, not through ``execute_restore``, because
the fixture Archive's Dump would have to hold a full WordPress schema; the phase
order is asserted over the built plan in ``tests/unit/adapters/
test_wpcli_extension.py``. Skips without the CI database, ``wp`` or ``php``;
a ``php`` without ``mysqli`` or a failed core download skips locally and
**fails** when ``CI`` is set (``ci.yml`` installs ``php-mysql`` for it).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.noop import ActionLog, NoopSearchReplace
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import WpCliSearchReplace
from l3io.wp.backup.domain.errors import WpCliConfigFoundError
from l3io.wp.backup.domain.search_replace import (
    UNRESOLVED_URL,
    Replacement,
    UrlRelocation,
)
from tests.integration.live_restore import host, port, root_connection

#: The WordPress release the fixture site runs (any recent one will do).
WORDPRESS_VERSION = "6.8.3"
OLD = "http://old.example.com"
NEW = "https://relocated-site.example.org"  # a different length from OLD

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


def _wp() -> str:
    wp = shutil.which("wp")
    if wp is None:
        pytest.skip("needs WP-CLI on PATH (CI installs the pinned release)")
    php = shutil.which("php")
    if php is None:
        pytest.skip("needs php")
    probe = subprocess.run(  # noqa: S603 - a fixed argv
        [php, "-r", "exit(extension_loaded('mysqli') ? 0 : 1);"],
        env={"PATH": os.environ["PATH"]},
        check=False,
    )
    if probe.returncode != 0:
        _missing("php has no mysqli extension (needs php-mysql)")
    return wp


def _missing(reason: str) -> None:
    """Skip locally; in CI (``CI`` set) fail, so this file can never silently skip.

    ``ci.yml``'s "install mysql client and WP-CLI" step installs ``wp``,
    ``php-cli`` and ``php-mysql``; a missing prerequisite there is a broken
    pipeline, not a reason to check nothing.
    """
    if os.environ.get("CI"):
        pytest.fail(f"{reason}, and CI is set: the search-replace tests cannot run")
    pytest.skip(reason)


def _root_flag() -> list[str]:
    return ["--allow-root"] if os.geteuid() == 0 else []


def _run_wp(wp: str, cwd: Path, *args: str) -> str:
    """The test's own ``wp`` call: minimal env, no config, no plugins or themes."""
    empty = cwd / "empty-config.yml"
    empty.touch()
    result = subprocess.run(  # noqa: S603 - a fixed argv
        [
            wp,
            *_root_flag(),
            "--skip-plugins",
            "--skip-themes",
            "--skip-packages",
            *args,
        ],
        cwd=cwd,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/"),
            "LC_ALL": "C",
            "WP_CLI_CONFIG_PATH": str(empty),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


@pytest.fixture(scope="session")
def wordpress_core(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wp = _wp()
    core = tmp_path_factory.mktemp("wordpress-core")
    cwd = tmp_path_factory.mktemp("wp-cwd")
    try:
        _run_wp(
            wp,
            cwd,
            "core",
            "download",
            f"--version={WORDPRESS_VERSION}",
            "--skip-content",
            f"--path={core}",
        )
    except AssertionError as exc:
        _missing(f"could not download WordPress {WORDPRESS_VERSION}: {exc}")
    return core


def _s(text: str) -> str:
    """A PHP serialized string, ``s:<bytes>:"<text>";`` (fixture building only)."""
    return f's:{len(text.encode())}:"{text}";'


SERIALIZED = 'a:2:{s:3:"url";s:22:"http://old.example.com";s:1:"n";i:1;}'
WIDGET_TEXT = f'<a href="{OLD}/about">About</a>'
WIDGET = "a:1:{i:2;a:1:{" + _s("text") + _s(WIDGET_TEXT) + "}}"
META = "a:1:{" + _s("link") + _s(f"{OLD}/wp-content/uploads/a.jpg") + "}"


@dataclass
class Site:
    wp: str
    root: Path
    database: str
    cwd: Path
    post_id: int
    guid: str


def _config(database: str, *, home: str | None) -> str:
    home_line = "" if home is None else f"define( 'WP_HOME', '{home}' );\n"
    user = os.environ.get("DB_USER", "root")
    return (
        "<?php\n"
        f"define( 'DB_NAME', '{database}' );\n"
        f"define( 'DB_USER', '{user}' );\n"
        f"define( 'DB_PASSWORD', '{os.environ['DB_PASSWORD']}' );\n"
        f"define( 'DB_HOST', '{host()}:{port()}' );\n"
        "define( 'DB_CHARSET', 'utf8mb4' );\n"
        f"{home_line}"
        "$table_prefix = 'wp_';\n"
        "if ( ! defined( 'ABSPATH' ) ) { define( 'ABSPATH', __DIR__ . '/' ); }\n"
        "require_once ABSPATH . 'wp-settings.php';\n"
    )


@pytest.fixture
def site(wordpress_core: Path, tmp_path: Path) -> Iterator[Site]:
    wp = _wp()
    database = f"wpsr_{uuid.uuid4().hex[:10]}"
    root = tmp_path / "parent" / "site"
    shutil.copytree(wordpress_core, root)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    with root_connection().cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{database}`")
    try:
        (root / "wp-config.php").write_text(_config(database, home=None), "utf-8")
        _run_wp(
            wp,
            cwd,
            f"--path={root}",
            "core",
            "install",
            f"--url={OLD}",
            "--title=fixture",
            "--admin_user=fixture",
            f"--admin_password={uuid.uuid4().hex}",
            "--admin_email=fixture@example.com",
            "--skip-email",
        )
        post_id = _seed(database)
        with root_connection(database).cursor() as cursor:
            cursor.execute("SELECT guid FROM wp_posts WHERE ID = %s", (post_id,))
            (guid,) = cursor.fetchone()
        # REWRITE_CONFIG has run by the time SEARCH_REPLACE does: WP_HOME is new.
        (root / "wp-config.php").write_text(_config(database, home=NEW), "utf-8")
        yield Site(wp, root, database, cwd, post_id, guid)
    finally:
        with root_connection().cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")


def _seed(database: str) -> int:
    with root_connection(database).cursor() as cursor:
        cursor.executemany(
            # A fresh install already creates `widget_text` (WordPress 6.8);
            # the seed replaces its value with one that carries the old URL.
            "INSERT INTO wp_options (option_name, option_value, autoload) "
            "VALUES (%s, %s, 'no') "
            "ON DUPLICATE KEY UPDATE option_value = VALUES(option_value)",
            [("l3io_sr_serialized", SERIALIZED), ("widget_text", WIDGET)],
        )
        cursor.execute(
            "INSERT INTO wp_posts (post_author, post_date, post_date_gmt, "
            "post_content, post_title, post_excerpt, post_status, post_name, "
            "to_ping, pinged, post_modified, post_modified_gmt, "
            "post_content_filtered, guid, post_type) VALUES (1, NOW(), NOW(), %s, "
            "'relocated', '', 'publish', 'relocated', '', '', NOW(), NOW(), '', %s, "
            "'post')",
            (f'<p><a href="{OLD}/page">link</a></p>', f"{OLD}/?p=999"),
        )
        post_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO wp_postmeta (post_id, meta_key, meta_value) "
            "VALUES (%s, 'l3io_meta', %s)",
            (post_id, META),
        )
        cursor.execute(
            "CREATE TABLE wp_plugin_x (id int PRIMARY KEY, data text NOT NULL)"
        )
        cursor.execute("INSERT INTO wp_plugin_x VALUES (1, %s)", (f"see {OLD}/plugin",))
    return post_id


def _oracle(database: str, needle: str = OLD) -> int:
    """Cells holding ``needle``: every text column of every ``wp_`` table but guid."""
    with root_connection(database).cursor() as cursor:
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name LIKE 'wp\\_%%' "
            "AND column_name <> 'guid' AND data_type IN "
            "('char','varchar','tinytext','text','mediumtext','longtext')",
            (database,),
        )
        columns = cursor.fetchall()
        total = 0
        for table, column in columns:
            cursor.execute(
                f"SELECT COUNT(*) FROM `{table}` WHERE "  # noqa: S608 - schema names
                f"INSTR(CAST(`{column}` AS BINARY), CAST(%s AS BINARY)) > 0",
                (needle,),
            )
            total += int(cursor.fetchone()[0])
    return total


def _plant_yml(directory: Path, marker: Path) -> None:
    (directory / "marker.php").write_text(
        f"<?php file_put_contents({str(marker)!r}, 'loaded');\n", encoding="utf-8"
    )
    (directory / "wp-cli.yml").write_text("require:\n  - marker.php\n", "utf-8")


def test_content_urls_are_rewritten_and_serialization_survives(
    site: Site, tmp_path: Path
) -> None:
    yml_markers = [tmp_path / "yml-site.txt", tmp_path / "yml-parent.txt"]
    _plant_yml(site.root, yml_markers[0])
    _plant_yml(site.root.parent, yml_markers[1])
    mu_marker = tmp_path / "mu-plugin-ran.txt"
    mu = site.root / "wp-content" / "mu-plugins"
    mu.mkdir(parents=True, exist_ok=True)
    (mu / "marker.php").write_text(
        f"<?php file_put_contents({str(mu_marker)!r}, 'ran');\n", encoding="utf-8"
    )
    before = _oracle(site.database)
    assert before >= 6, "the fixture did not seed the old URL"

    report = WpCliSearchReplace(site.wp, secrets=SecretRegistry()).relocate_content(
        site.root, UrlRelocation(site_url=NEW, home_url=NEW)
    )

    # Before this test's own wp calls, so both observations are the adapter's.
    for marker in yml_markers:
        assert not marker.exists(), f"a planted wp-cli.yml was loaded: {marker}"
    assert mu_marker.exists(), "mu-plugins run under WP-CLI (the stated residual)"
    assert report.pairs == (Replacement(OLD, NEW),), "option list read stored URLs"
    assert report.merged is True
    assert report.remaining == 0
    assert report.replacements == before
    assert {"wp_options", "wp_posts", "wp_postmeta", "wp_plugin_x"} <= set(
        report.tables or ()
    )
    assert _oracle(site.database) == 0
    assert _oracle(site.database, NEW) >= before

    path = f"--path={site.root}"
    got = json.loads(
        _run_wp(
            site.wp,
            site.cwd,
            path,
            "option",
            "get",
            "l3io_sr_serialized",
            "--format=json",
        )
    )
    assert got == {"url": NEW, "n": 1}
    widget = json.loads(
        _run_wp(
            site.wp, site.cwd, path, "option", "get", "widget_text", "--format=json"
        )
    )
    assert widget == {"2": {"text": WIDGET_TEXT.replace(OLD, NEW)}}
    meta = json.loads(
        _run_wp(
            site.wp,
            site.cwd,
            path,
            "post",
            "meta",
            "get",
            str(site.post_id),
            "l3io_meta",
            "--format=json",
        )
    )
    assert meta == {"link": f"{NEW}/wp-content/uploads/a.jpg"}
    with root_connection(site.database).cursor() as cursor:
        cursor.execute("SELECT guid FROM wp_posts WHERE ID = %s", (site.post_id,))
        assert cursor.fetchone()[0] == site.guid
        assert site.guid.startswith(OLD)


def test_a_wp_cli_yml_above_the_state_directory_is_refused(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = tmp_path / "statehome"
    state = holder / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    marker = tmp_path / "yml-state.txt"
    _plant_yml(holder, marker)
    before = _oracle(site.database)
    with pytest.raises(WpCliConfigFoundError):
        WpCliSearchReplace(site.wp, secrets=SecretRegistry()).relocate_content(
            site.root, UrlRelocation(site_url=NEW, home_url=None)
        )
    assert not marker.exists()
    assert _oracle(site.database) == before


def test_the_dry_run_twin_changes_nothing(site: Site) -> None:
    before = _oracle(site.database)
    report = NoopSearchReplace(ActionLog()).relocate_content(
        site.root, UrlRelocation(site_url=NEW, home_url=NEW)
    )
    assert report.pairs == (Replacement(UNRESOLVED_URL, NEW),)
    assert report.replacements is None
    assert _oracle(site.database) == before
