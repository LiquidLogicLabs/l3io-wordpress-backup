"""AD-3 — ``locate_wp_config`` searches where ``wp-load.php`` searches."""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.database import LocatedConfig, locate_wp_config
from l3io.wp.backup.domain.archive_format import ConfigLocation
from l3io.wp.backup.domain.errors import InvalidSiteDescriptorError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    site = tmp_path / "parent" / "site"
    site.mkdir(parents=True)
    return site


def _touch(path: Path) -> Path:
    path.write_text("<?php\n", encoding="utf-8")
    return path


def test_the_site_root_config_wins_over_the_parent(root: Path) -> None:
    in_root = _touch(root / "wp-config.php")
    _touch(root.parent / "wp-config.php")
    with capture_logs() as logs:
        located = locate_wp_config(root, stated=None)
    assert located == LocatedConfig(in_root.resolve(), ConfigLocation.SITE_ROOT)
    assert logs[-1]["event"] == "wp_config_located"
    assert logs[-1]["location"] == "wp-config.php"


def test_the_parent_is_used_without_wp_settings(root: Path) -> None:
    in_parent = _touch(root.parent / "wp-config.php")
    located = locate_wp_config(root, stated=None)
    assert located == LocatedConfig(in_parent.resolve(), ConfigLocation.PARENT)


def test_a_parent_holding_wp_settings_is_refused_naming_both(root: Path) -> None:
    _touch(root.parent / "wp-config.php")
    _touch(root.parent / "wp-settings.php")
    with pytest.raises(InvalidSiteDescriptorError) as caught:
        locate_wp_config(root, stated=None)
    assert caught.value.paths == (
        str(root.resolve() / "wp-config.php"),
        str(root.resolve().parent / "wp-config.php"),
    )


def test_nothing_found_is_refused_naming_both(root: Path) -> None:
    with pytest.raises(InvalidSiteDescriptorError) as caught:
        locate_wp_config(root, stated=None)
    assert len(caught.value.paths) == 2


def test_a_stated_path_wins_and_is_not_re_derived(root: Path, tmp_path: Path) -> None:
    _touch(root / "wp-config.php")
    (tmp_path / "configs").mkdir()
    elsewhere = _touch(tmp_path / "configs" / "site.php")
    with capture_logs() as logs:
        located = locate_wp_config(root, stated=elsewhere)
    assert located == LocatedConfig(elsewhere.resolve(), None)
    assert any(
        e["event"] == "wp_config_outside_search_path"
        and e["log_level"] == "warning"
        and e["path"] == str(elsewhere.resolve())
        for e in logs
    )


def test_a_stated_path_in_a_search_location_reports_it(root: Path) -> None:
    in_parent = _touch(root.parent / "wp-config.php")
    _touch(root / "wp-config.php")
    located = locate_wp_config(root, stated=in_parent)
    assert located == LocatedConfig(in_parent.resolve(), ConfigLocation.PARENT)


def test_a_stated_symlink_is_canonicalised(root: Path, tmp_path: Path) -> None:
    target = _touch(root / "wp-config.php")
    link = tmp_path / "link.php"
    link.symlink_to(target)
    located = locate_wp_config(root, stated=link)
    assert located == LocatedConfig(target.resolve(), ConfigLocation.SITE_ROOT)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_an_unusable_stated_path_is_refused_and_nothing_is_searched(
    root: Path, tmp_path: Path, kind: str
) -> None:
    _touch(root / "wp-config.php")
    stated = tmp_path / "stated.php"
    if kind == "directory":
        stated.mkdir()
    with pytest.raises(InvalidSiteDescriptorError) as caught:
        locate_wp_config(root, stated=stated)
    assert caught.value.paths == (str(stated),)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_an_unusable_site_root_is_refused(tmp_path: Path, kind: str) -> None:
    site = tmp_path / "site"
    if kind == "file":
        _touch(site)
    with pytest.raises(InvalidSiteDescriptorError):
        locate_wp_config(site, stated=None)
