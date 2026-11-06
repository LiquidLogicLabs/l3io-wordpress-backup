"""Regenerate the committed synthetic v0 fixture Archives (FR-10, AD-23).

Run from the project root::

    uv run python tests/fixtures/archives/v0/make_v0_fixtures.py

It writes only below this directory. The test suite imports :func:`build` and
writes into ``tmp_path`` instead (``tests/contracts/test_v0_fixtures_reproducible.py``
compares the two byte for byte), and :func:`write_v0` is what the AD-21 case in
``tests/contracts/test_streaming.py`` builds its large v0 Archive with. The
package never imports this module.

**What it mirrors.** The legacy writer at tag ``baseline-pre-rewrite``
(``wpbackup2/classes/wp_internal_backup.py`` L80-97)::

    with tarfile.open(archive_filename, 'w:gz') as stream:
        stream.add(db_dump_filename, arcname='database.sql')   # --mode all only
        stream.add(site_path, arcname='wp-root')

so a ``database.sql`` member, then a ``wp-root`` directory member, then
``wp-root/<path>`` members in ``tarfile.add``'s sorted recursion, symlinks stored
as links and a multiply-linked file stored as a hard link (``LNKTYPE``). The two
member names are spelled here, not imported from ``domain/archive_format.py``:
they are the legacy tool's, and this module is the independent statement of
what it wrote.

**What it changes, for determinism only.** ``'w:gz'`` stamps the current time
into the gzip header, so the gzip stream is opened as
``gzip.GzipFile(filename="", mode="wb", mtime=0)``; and a ``filter`` fixes each
member's mtime, mode, uid/gid and owner names, which the legacy tool took from
the disk. Neither changes a member name, type or body.

**The malformed set** (``malformed/``) is crafted with ``tarfile.TarInfo``, still
through ``tarfile``: shapes verification must refuse. ``hardlink.tar.gz`` is the
exception: it comes from a real multiply-linked file through the same
``tarfile.add`` path, because that is how 0.2.21 wrote one.
"""

from __future__ import annotations

import gzip
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).resolve().parent
PROJECT_ROOT: Final = HERE.parents[3]
WP_CONFIG: Final = PROJECT_ROOT / "tests" / "data" / "wp-config.php"

#: The legacy tool's member names (``DB_DUMP_ARCNAME``, ``WP_DIR_ARCNAME``).
LEGACY_DUMP: Final = "database.sql"
LEGACY_SITE_ROOT: Final = "wp-root"

#: The fixed member mtime (2024-01-01T00:00:00Z) and modes.
MTIME: Final = 1_704_067_200
FILE_MODE: Final = 0o644
DIR_MODE: Final = 0o755
LINK_MODE: Final = 0o777

DUMP: Final = (
    b"-- MySQL dump (synthetic v0 fixture)\n"
    b"DROP TABLE IF EXISTS `wp_options`;\n"
    b"CREATE TABLE `wp_options` (`option_id` INT, `option_name` VARCHAR(191));\n"
    b"INSERT INTO `wp_options` VALUES (1,'siteurl');\n"
)
INDEX: Final = b"<?php\n// synthetic v0 fixture\n"
UPLOAD: Final = b"an upload\n"
#: A directory path long enough that its file's member name passes 100 bytes.
LONG_DIR: Final = (
    "wp-content/plugins/" + "a-plugin-with-a-very-long-directory-name/" * 2
)
LONG_FILE: Final = "a-file-whose-name-is-long-too.php"
#: The escaping member of ``escape-dotdot``: a traversal whose name also carries
#: a newline and an ANSI escape, so logs prove they render it escaped.
ESCAPE_DOTDOT: Final = f"{LEGACY_SITE_ROOT}/../escape\n\x1b[31mred"
#: ``escape-absolute``: the absolute path spelled inside the v0 prefix (F-9).
ESCAPE_ABSOLUTE: Final = f"{LEGACY_SITE_ROOT}//etc/x"

#: Every Archive :func:`build` writes, relative to its output directory.
SYNTHETIC: Final = (
    "synthetic-all-pax.tar.gz",
    "synthetic-all-gnu.tar.gz",
    "synthetic-all-symlink.tar.gz",
    "synthetic-all-longname.tar.gz",
    "synthetic-files-only.tar.gz",
    "synthetic-renamed.bak",
    "symlink-escape.tar.gz",
)
MALFORMED: Final = (
    "malformed/missing-wp-root.tar.gz",
    "malformed/stray-manifest-late.tar.gz",
    "malformed/escape-dotdot.tar.gz",
    "malformed/escape-absolute.tar.gz",
    "malformed/hardlink.tar.gz",
    "malformed/truncated.tar.gz",
)

SiteMaker = Callable[[Path], None]


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.issym():
        info.mode = LINK_MODE
    elif info.isdir():
        info.mode = DIR_MODE
    else:
        info.mode = FILE_MODE
    return info


def _open(path: Path) -> tuple[io.BufferedWriter, gzip.GzipFile]:
    raw = path.open("wb")
    return raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)


def write_v0(
    path: Path,
    site_root: Path,
    dump: Path | None,
    *,
    tar_format: int = tarfile.PAX_FORMAT,
) -> Path:
    """Write ``site_root`` (and ``dump``) as 0.2.21 did, deterministically."""
    raw, gz = _open(path)
    with raw, gz, tarfile.open(fileobj=gz, mode="w", format=tar_format) as stream:
        if dump is not None:
            stream.add(dump, arcname=LEGACY_DUMP, filter=_normalise)
        stream.add(site_root, arcname=LEGACY_SITE_ROOT, filter=_normalise)
    return path


def write_crafted(
    path: Path, members: list[tarfile.TarInfo | tuple[str, bytes]]
) -> Path:
    """Write ``members`` as given: names, types and bodies verbatim."""
    raw, gz = _open(path)
    with raw, gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as out:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                out.addfile(_normalise(member))
                continue
            name, data = member
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(_normalise(info), io.BytesIO(data))
    return path


def make_site(root: Path) -> None:
    """The input Site Root every passing fixture starts from."""
    (root / "wp-content" / "uploads").mkdir(parents=True)
    (root / "wp-content" / "empty").mkdir()
    shutil.copyfile(WP_CONFIG, root / "wp-config.php")
    (root / "index.php").write_bytes(INDEX)
    (root / "wp-content" / "uploads" / "a.txt").write_bytes(UPLOAD)


def _with_symlink(root: Path) -> None:
    make_site(root)
    (root / "latest.php").symlink_to("index.php")


def _with_longname(root: Path) -> None:
    make_site(root)
    (root / LONG_DIR).mkdir(parents=True)
    (root / LONG_DIR / LONG_FILE).write_bytes(INDEX)


def _with_escaping_link(root: Path) -> None:
    make_site(root)
    (root / "link").symlink_to("../../outside")


def _with_hard_link(root: Path) -> None:
    make_site(root)
    os.link(root / "index.php", root / "x")


def _site_archive(
    scratch: Path,
    out: Path,
    maker: SiteMaker,
    *,
    dump: bool = True,
    tar_format: int = tarfile.PAX_FORMAT,
) -> Path:
    work = Path(tempfile.mkdtemp(dir=scratch))
    root = work / "site"
    root.mkdir()
    maker(root)
    dump_file = work / "dump.sql"
    dump_file.write_bytes(DUMP)
    return write_v0(out, root, dump_file if dump else None, tar_format=tar_format)


def _member(name: str, data: bytes) -> tuple[str, bytes]:
    return name, data


def _directory(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


def _v0_members() -> list[tarfile.TarInfo | tuple[str, bytes]]:
    return [
        _member(LEGACY_DUMP, DUMP),
        _directory(LEGACY_SITE_ROOT),
        _member(f"{LEGACY_SITE_ROOT}/index.php", INDEX),
        _member(f"{LEGACY_SITE_ROOT}/wp-config.php", WP_CONFIG.read_bytes()),
    ]


def build(out: Path) -> list[Path]:
    """Write every synthetic and malformed Archive below ``out``; their paths."""
    malformed = out / "malformed"
    malformed.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        pax = _site_archive(scratch, out / "synthetic-all-pax.tar.gz", make_site)
        _site_archive(
            scratch,
            out / "synthetic-all-gnu.tar.gz",
            make_site,
            tar_format=tarfile.GNU_FORMAT,
        )
        _site_archive(scratch, out / "synthetic-all-symlink.tar.gz", _with_symlink)
        _site_archive(scratch, out / "synthetic-all-longname.tar.gz", _with_longname)
        _site_archive(
            scratch, out / "synthetic-files-only.tar.gz", make_site, dump=False
        )
        shutil.copyfile(pax, out / "synthetic-renamed.bak")
        _site_archive(
            scratch, out / "symlink-escape.tar.gz", _with_escaping_link, dump=False
        )
        _site_archive(scratch, malformed / "hardlink.tar.gz", _with_hard_link)
    write_crafted(malformed / "missing-wp-root.tar.gz", [_member(LEGACY_DUMP, DUMP)])
    write_crafted(
        malformed / "stray-manifest-late.tar.gz",
        [*_v0_members(), _member("manifest.json", b'{"format_version": 1}')],
    )
    write_crafted(
        malformed / "escape-dotdot.tar.gz",
        [*_v0_members(), _member(ESCAPE_DOTDOT, b"must never be written\n")],
    )
    write_crafted(
        malformed / "escape-absolute.tar.gz",
        [*_v0_members(), _member(ESCAPE_ABSOLUTE, b"must never be written\n")],
    )
    data = pax.read_bytes()
    (malformed / "truncated.tar.gz").write_bytes(data[: len(data) // 2])
    return [out / name for name in (*SYNTHETIC, *MALFORMED)]


if __name__ == "__main__":
    build(HERE)
