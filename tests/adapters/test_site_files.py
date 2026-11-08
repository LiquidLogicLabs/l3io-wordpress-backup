"""FR-7/FR-9 — ``LocalSiteFiles``: emptiness, clearing, extraction.

Runs as any user, root included: nothing here depends on a permission bit
being enforced — a failing removal is forced through a double.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import site_files as site_files_module
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.domain.archive_format import MemberRole
from l3io.wp.backup.domain.errors import (
    CleanFailedError,
    ExtractionRefusedError,
    InvalidSiteDescriptorError,
    RefusedError,
    UnreadableSiteRootError,
    UnsafeCleanTargetError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.site_files import SiteFilesCommand
from tests.contracts.test_extraction import _file, _v1

CONFIG = b"<?php\r\ndefine('DB_NAME', 'kept');\r\n"


@pytest.fixture
def files() -> LocalSiteFiles:
    return LocalSiteFiles()


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    (root / "wp-includes").mkdir(parents=True)
    (root / "wp-includes" / "version.php").write_text("<?php\n", encoding="utf-8")
    (root / "wp-config.php").write_bytes(CONFIG)
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "wp-content" / "uploads" / "a.jpg").write_bytes(b"jpg")
    (root / ".htaccess").write_text("# rules\n", encoding="utf-8")
    return root


def test_it_satisfies_the_command_protocol(files: LocalSiteFiles) -> None:
    assert isinstance(files, SiteFilesCommand)


def test_emptiness_and_count(files: LocalSiteFiles, site: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert files.is_empty(empty)
    assert files.entry_count(empty) == 0
    assert not files.is_empty(site)
    assert files.entry_count(site) == 5


def test_file_exists_follows_symlinks(files: LocalSiteFiles, tmp_path: Path) -> None:
    regular = tmp_path / "f"
    regular.write_text("x", encoding="utf-8")
    to_file = tmp_path / "to-file"
    to_file.symlink_to(regular)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    assert files.file_exists(regular)
    assert files.file_exists(to_file)
    assert not files.file_exists(dangling)
    assert not files.file_exists(tmp_path / "missing")


def test_clean_honours_keep_and_never_removes_the_root(
    files: LocalSiteFiles, site: Path
) -> None:
    with capture_logs() as logs:
        removed = files.clean(
            site,
            keep=frozenset(
                {PurePosixPath("wp-config.php"), PurePosixPath("wp-content")}
            ),
        )
    assert removed == 3
    assert site.is_dir()
    assert sorted(p.name for p in site.iterdir()) == ["wp-config.php", "wp-content"]
    assert (site / "wp-config.php").read_bytes() == CONFIG
    assert (site / "wp-content" / "uploads" / "a.jpg").read_bytes() == b"jpg"
    (record,) = [e for e in logs if e["event"] == "site_root_cleaned"]
    assert record["removed"] == 3
    assert "duration_ms" in record


def test_keep_matches_names_case_insensitively_and_after_nfc(
    files: LocalSiteFiles, site: Path
) -> None:
    """RT-005 (BL-E003-040): on a case-insensitive or normalising filesystem
    the differently spelled entry *is* the kept one, so it is never removed."""
    (site / "wp-config.php").rename(site / "WP-Config.PHP")
    decomposed = "cafe\u0301"  # NFD of "café"
    (site / decomposed).mkdir()
    files.clean(
        site,
        keep=frozenset({PurePosixPath("wp-config.php"), PurePosixPath("caf\u00e9")}),
    )
    assert sorted(p.name for p in site.iterdir()) == sorted(
        ["WP-Config.PHP", decomposed]
    )
    assert (site / "WP-Config.PHP").read_bytes() == CONFIG


def test_a_symlinked_directory_is_unlinked_and_its_target_untouched(
    files: LocalSiteFiles, site: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep me", encoding="utf-8")
    (site / "linked").symlink_to(outside, target_is_directory=True)
    files.clean(site, keep=frozenset({PurePosixPath("wp-config.php")}))
    assert not (site / "linked").exists()
    assert not (site / "linked").is_symlink()
    assert (outside / "precious").read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("which", ["root", "home"])
def test_root_and_home_are_refused(
    files: LocalSiteFiles,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    if which == "root":
        target = Path("/")
    else:
        # A home that even looks like a Site: still refused, and untouched.
        target = tmp_path / "home"
        target.mkdir()
        (target / "wp-config.php").write_text("<?php\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(target))
    before = sorted(p.name for p in target.iterdir())

    def never(*_: object, **__: object) -> None:
        msg = "clean removed something past its guard"
        raise AssertionError(msg)

    # A broken guard must fail this test, never wipe the (root) runner.
    monkeypatch.setattr(site_files_module.shutil, "rmtree", never)
    monkeypatch.setattr(Path, "unlink", never)
    with pytest.raises(UnsafeCleanTargetError) as caught:
        files.clean(target, keep=frozenset())
    assert isinstance(caught.value, RefusedError)
    assert sorted(p.name for p in target.iterdir()) == before


def test_a_directory_that_is_not_a_site_is_refused(
    files: LocalSiteFiles, tmp_path: Path
) -> None:
    stray = tmp_path / "documents"
    stray.mkdir()
    (stray / "thesis.pdf").write_bytes(b"pdf")
    with pytest.raises(UnsafeCleanTargetError, match=r"neither wp-config\.php"):
        files.clean(stray, keep=frozenset())
    assert (stray / "thesis.pdf").exists()


def test_wp_includes_alone_marks_a_site(files: LocalSiteFiles, site: Path) -> None:
    (site / "wp-config.php").unlink()
    assert files.clean(site, keep=frozenset({PurePosixPath("wp-includes")})) == 3


def test_an_oserror_mid_clean_stops_and_names_the_entry(
    files: LocalSiteFiles, site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rmtree = shutil.rmtree
    removed: list[str] = []

    def rmtree(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name == "wp-content":
            raise PermissionError(13, "Permission denied")
        removed.append(Path(path).name)
        real_rmtree(path)

    monkeypatch.setattr(site_files_module.shutil, "rmtree", rmtree)
    with pytest.raises(CleanFailedError) as caught:
        files.clean(site, keep=frozenset())
    # Sorted order: .htaccess, index.php, wp-config.php, wp-content, wp-includes.
    assert caught.value.entry == site / "wp-content"
    assert caught.value.removed == 3
    assert "Permission denied" in str(caught.value)
    assert (site / "wp-content").exists()
    assert (site / "wp-includes").exists()  # after the failure: left alone
    assert removed == []


def test_a_nested_keep_path_is_refused_before_anything_is_removed(
    files: LocalSiteFiles, site: Path
) -> None:
    """BL-E002-088: it could only keep its whole ancestor (old plugins too)."""
    with pytest.raises(ValueError, match="top-level"):
        files.clean(site, keep=frozenset({PurePosixPath("wp-content/uploads")}))
    assert files.entry_count(site) == 5


@pytest.mark.parametrize("bad", ["/etc", "../x"])
def test_keep_must_be_site_relative(
    files: LocalSiteFiles, site: Path, bad: str
) -> None:
    with pytest.raises(ValueError, match="keep"):
        files.clean(site, keep=frozenset({PurePosixPath(bad)}))
    assert files.entry_count(site) == 5


def test_extract_excluding_the_config_leaves_it_byte_identical(
    files: LocalSiteFiles, tmp_path: Path
) -> None:
    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "wp-config.php").write_bytes(CONFIG)
    source, view = _v1(
        tmp_path,
        [
            _file("site-root/index.php", b"<?php"),
            _file("site-root/wp-config.php", b"<?php // the archive's"),
        ],
    )
    result = files.extract(
        source,
        destination,
        view,
        roles=frozenset({MemberRole.SITE_ROOT}),
        exclude=frozenset({PurePosixPath("wp-config.php")}),
    )
    assert (destination / "wp-config.php").read_bytes() == CONFIG
    assert (destination / "index.php").read_bytes() == b"<?php"
    assert result.members == 1


def test_a_case_variant_of_the_kept_config_is_refused_where_it_is_the_same_file(
    files: LocalSiteFiles, tmp_path: Path
) -> None:
    """BL-E002-067: on a case-insensitive filesystem ``WP-CONFIG.PHP`` *is*
    ``wp-config.php``. A hard link gives the two names one inode here, which
    is what such a filesystem does; the kept file stays byte-identical."""
    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "wp-config.php").write_bytes(CONFIG)
    os.link(destination / "wp-config.php", destination / "WP-CONFIG.PHP")
    source, view = _v1(
        tmp_path,
        [
            _file("site-root/index.php", b"<?php"),
            _file("site-root/WP-CONFIG.PHP", b"<?php // the archive's"),
        ],
    )
    with pytest.raises(ExtractionRefusedError, match="excluded path"):
        files.extract(
            source,
            destination,
            view,
            roles=frozenset({MemberRole.SITE_ROOT}),
            exclude=frozenset({PurePosixPath("wp-config.php")}),
        )
    assert (destination / "wp-config.php").read_bytes() == CONFIG


def test_a_case_variant_that_is_another_file_is_extracted(
    files: LocalSiteFiles, tmp_path: Path
) -> None:
    """On a case-sensitive filesystem the variant is a different file."""
    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "wp-config.php").write_bytes(CONFIG)
    source, view = _v1(tmp_path, [_file("site-root/WP-CONFIG.PHP", b"other")])
    files.extract(
        source,
        destination,
        view,
        roles=frozenset({MemberRole.SITE_ROOT}),
        exclude=frozenset({PurePosixPath("wp-config.php")}),
    )
    assert (destination / "wp-config.php").read_bytes() == CONFIG
    assert (destination / "WP-CONFIG.PHP").read_bytes() == b"other"


def test_extract_delegates_to_extract_archive_unchanged(
    files: LocalSiteFiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []
    sentinel = object()

    def extract_archive(*args: object, **kwargs: object) -> object:
        seen.append((*args, kwargs))
        return sentinel

    monkeypatch.setattr(site_files_module, "extract_archive", extract_archive)
    roles = frozenset({MemberRole.SITE_ROOT})
    exclude = frozenset({PurePosixPath("wp-config.php")})
    payload = tmp_path / "payload"
    got = files.extract(
        "src",  # type: ignore[arg-type]
        tmp_path,
        "view",  # type: ignore[arg-type]
        roles=roles,
        exclude=exclude,
        payload=payload,
    )
    assert got is sentinel
    assert seen == [
        (
            "src",
            tmp_path,
            "view",
            {"roles": roles, "exclude": exclude, "payload": payload},
        )
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores a directory's mode")
def test_file_exists_is_false_where_the_path_cannot_be_searched(
    files: LocalSiteFiles, tmp_path: Path
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f").write_text("x", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert files.file_exists(locked / "f") is False
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize("method", ["is_empty", "entry_count"])
def test_a_missing_site_root_is_a_package_error(
    files: LocalSiteFiles, tmp_path: Path, method: str
) -> None:
    missing = tmp_path / "no-such-site"
    with pytest.raises(InvalidSiteDescriptorError) as caught:
        getattr(files, method)(missing)
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert str(missing) in caught.value.paths


def test_a_site_root_that_cannot_be_listed_is_refused(
    files: LocalSiteFiles, site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E003-034: present and unreadable is a refusal, never "empty"."""

    def scandir(path: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(site_files_module.os, "scandir", scandir)
    with pytest.raises(UnreadableSiteRootError, match="PermissionError") as caught:
        files.clean(site, keep=frozenset())
    assert caught.value.exit_code is ExitCode.REFUSED
    assert not isinstance(caught.value, InvalidSiteDescriptorError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores a mode-000 directory")
@pytest.mark.parametrize("query", ["is_empty", "entry_count"])
def test_a_mode_000_site_root_is_refused_not_read_as_empty(
    files: LocalSiteFiles, site: Path, query: str
) -> None:
    site.chmod(0o000)
    try:
        with pytest.raises(UnreadableSiteRootError) as caught:
            getattr(files, query)(site)
    finally:
        site.chmod(0o755)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert str(site) in str(caught.value)
