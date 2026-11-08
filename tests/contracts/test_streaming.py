"""AD-21 — Archives stream; nothing buffers a whole Archive.

The rule: every port that moves Archive bytes exposes a file-object interface,
never ``bytes`` (asserted by ``test_port_types.py``), and peak RSS is bounded
by a constant independent of Archive size. This file asserts the second half
against the **real backup path** — ``execute_backup_plan`` with the real
traversal, writer, store and verifier (``verify_or_raise``), and a database
double that copies a Dump file in chunks — over a site whose Site Root and Dump total at
least ten times :data:`RSS_BOUND_BYTES`. The fixture is incompressible, so the
payload, the Archive copy and the verifier's read each move at least ten times
the bound too; both the indexed input and the published Archive are asserted
to, so a shrunken fixture cannot pass by moving little. A planted regression —
``tarfile``'s copy replaced by a whole-buffer read inside the real path — is
asserted to breach the bound, so the test can see the path it claims to cover.
The published Archive is then read back along the **real restore path** in a
second child: the probe, ``decrypt_payload`` (through a chunked pass-through
cipher), ``extract_archive`` from the decrypted payload, and ``read_member``
reaching the last member through the outer tar's payload.

**Encryption on and off (E004-S01-003, AG-13).** The same test runs over an
``encrypted`` axis. On, the backup encrypts with the real ``PassphraseCipher``
and the read-back decrypts with it, then streams the decrypted payload through
``read_inner_manifest`` and checks it against the outer Manifest. ``age``'s
passphrase recipient is scrypt, whose memory cost (hundreds of MiB inside
database's backend) is independent of the stream's size but not fixed: one
call peaks at 256 MiB, the next at 512 MiB. Subtracting a small run's growth
from a large run's, as this leg first did, left that difference in the result
and failed by 255 MiB under parallel CI. So the child leaves each cipher call's
**KDF window** out of the peak instead: on entry to ``encrypt``/``decrypt`` it
keeps the high-water mark so far, and at the call's first write (the ``age``
header, or the first plaintext chunk, both after scrypt has run and been freed)
it resets the mark to the current RSS. The rest of the run is measured directly
against the bound. Memory still resident at the first write stays counted, so a
payload read whole before encrypting still breaches; the test asserts one
window per leg and that the payload streamed after it. Not covered: a transient
allocation made and freed inside the window, before the cipher writes anything.

**Reach.** The RSS test covers the byte movers in :data:`RSS_COVERED` (the
traversal and writer, ``TarArchiveWriter`` included, the local store, and the
Dry Run's no-op command halves in ``noop.py``, measured over the same
fixture). A tripwire walks every module under
``adapters/`` (recursively, whatever its name) and fails on any other module
with the *capability* to move Archive bytes, detected from the AST: it imports
``tarfile`` or ``gzip`` (any spelling), or defines a function named like an
``ArchiveStoreCommand`` member (derived from the Protocol). A covered module's
top-level functions and classes are pinned too, so new code added *inside*
``tar.py`` or ``store.py`` (E002-S01-003's verifier, say) fails until its story
extends the RSS test. Every remote store trips the module tripwire the same
way.

The instrument is calibrated in both directions, because an RSS assertion that
cannot tell a streaming copy from a whole-file read reports success over
everything: a chunked copy stays under the bound, and a deliberate whole-file
read breaches it. Every measurement runs in a **fresh child interpreter** and
resets that child's RSS high-water mark to its current RSS immediately before
the work (Linux ``/proc/self/clear_refs``, then ``VmHWM``). A high-water mark
read without the reset is masked by whatever raised it earlier — an earlier
test, or the interpreter's own start-up — and measured that way a 64 MiB
whole-file read grew the peak by only 45 MiB; the bound would pass on a
broken meter. The measurement needs Linux's ``/proc``; elsewhere it skips.

Not covered: memory that grows with the **number** of Site entries (the
Manifest's member index is held whole, by design — the fixture is one large
file), a writer that buffers only for Archives larger than the fixture, and a
byte mover reached through ``importlib``/``__import__``.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from l3io.wp.backup.ports.archive import ArchiveStoreCommand
from tests.fixtures.archives.v0 import make_v0_fixtures

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PORTS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "ports"
ADAPTERS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "adapters"

#: Libraries whose import gives an adapter the means to move Archive bytes.
ARCHIVE_LIBRARIES = frozenset({"tarfile", "gzip"})
#: The Archive store command's members, derived from the Protocol itself.
STORE_COMMAND_MEMBERS = frozenset(
    name
    for name, member in vars(ArchiveStoreCommand).items()
    if inspect.isfunction(member) and not name.startswith("_")
)
#: Byte movers under ``adapters/`` that the RSS test below drives, each with
#: the exact set of top-level functions and classes it held when the test was
#: written. A covered module that gains or loses one fails
#: ``test_covered_modules_hold_only_what_the_rss_test_drives``, so new code in a
#: covered module (E002-S01-003's verifier in ``tar.py``, say) cannot ride on
#: this coverage unreviewed: extend the RSS test, then this set.
RSS_COVERED: dict[str, frozenset[str]] = {
    "tar.py": frozenset(
        {
            "ArchiveMemberSource",
            # RT-2: refuses an oversized extended-header body before tarfile
            # reads it; bounded by tests/adapters/test_tar_extended_header_bound.py.
            "TarArchiveProbe",
            "_BoundedTarInfo",
            "_Counter",
            "_ExtractFilter",
            "_existing_identity",  # BL-E002-067: one stat per member, no bytes
            # E008-S01-001 (BL-E004-053): called by TarArchiveWriter.write on
            # the real backup path this test drives; it serialises the
            # Manifest the writer builds anyway (members, not bytes).
            "_refuse_oversized_manifest",
            "_Scan",
            "_archive_path",
            "_began_as_a_tar",
            "_check_exclude",
            "_clean_trailer",
            "_digest",
            "_inner",
            "_open",
            # The outer-tar reader of both encodings (owner decision 21): driven
            # by the read-back's decrypt_payload and read_member below.
            "_outer_tar",
            "_payload_body",
            "_reason",
            "_stream_outer",
            # E003-S01-001: an interpreter capability check; moves no bytes.
            "require_patched_tarfile",
            "_tar_type",
            "_walk",
            "decrypt_payload",
            "extract_archive",
            "verify_archive",
            # verify_archive's body; driven through verify_or_raise below.
            "verify_archive_view",
            # verify_archive_view's body (BL-E002-115): the same single scan.
            "verify_archive_manifest",
            "verify_or_raise",
            # BL-E002-115: driven on the encrypted leg's restore path below.
            "read_inner_manifest",
            "_inner_manifest_failure",
            "_is_reserved",
            # The writer's inner Manifest, on the encrypted leg's backup.
            "_add_inner_manifest",
            "SiteRootCollection",
            "TarArchiveWriter",
            "_HashingWriter",
            "_add",
            "_add_file",
            "_decide",
            "_encrypt",
            "_header",
            "_header_kind",
            "_identity",
            "_is_outward",
            "_member_changed",
            "_outer_header",
            "_scan",
            "_write_outer",
            "_write_payload",
            "collect_site_root",
            "file_member",
            "write_archive",
        }
    ),
    # The Dry Run's command halves: driven by the "backup-noop" leg.
    "noop.py": frozenset(
        {
            "ActionLog",
            "GuardedTracer",
            "NoopArchiveStore",
            "NoopArchiveWriter",
            "NoopDatabase",
            "NoopTracer",
            "SkippedCall",
            "_Failed",
            "_GuardedPhaseSpan",
            "_GuardedRunSpan",
            "_NoopStagedArchive",
            # The restore twins (E002-S02-012) open no file and read no
            # source, so they move no bytes for this leg to measure;
            # tests/unit/adapters/test_noop_restore.py asserts they touch
            # nothing. ``_skip``/``_names`` build their log records.
            "NoopConfigWriter",
            "NoopRunJournal",
            "NoopSearchReplace",
            "NoopSiteFiles",
            "_names",
            "_skip",
        }
    ),
    "store.py": frozenset(
        {
            "LocalArchiveStore",
            "_DestinationWriter",
            "_LocalStagedArchive",
            "_final_path",
            "_occupied",
            "_reason",
        }
    ),
}
CLEAR_REFS = Path("/proc/self/clear_refs")

linux_proc = pytest.mark.skipif(
    not CLEAR_REFS.exists(), reason="RSS high-water reset needs Linux /proc"
)

MIB = 1024 * 1024
FIXTURE_BYTES = 64 * MIB
CHUNK_BYTES = 256 * 1024

#: The bound a streaming path must stay under, independent of fixture size.
RSS_BOUND_BYTES = 24 * MIB
#: What a whole-file read must exceed, or the instrument is not measuring.
RSS_TEETH_BYTES = 48 * MIB
#: The real path's fixture: Site Root file and Dump, each this size, random.
SITE_FILE_BYTES = 128 * MIB
DUMP_BYTES = 128 * MIB
#: The planted-regression run's Site Root file; big enough to breach the bound.
PLANTED_BYTES = 64 * MIB

#: Runs one measured action in a fresh interpreter and prints, on its last
#: line, ``{"growth": <peak RSS growth in bytes>, ...}``.
_CHILD = r"""
import json, shutil, sys
from pathlib import Path

def status(key):
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1]) * 1024
    raise SystemExit(f"no {key} in /proc/self/status")

#: High-water marks of the measured run's spans closed before a KDF window.
closed = []

def baseline():
    # Reset the high-water mark to the current RSS, and return it.
    closed.clear()
    Path("/proc/self/clear_refs").write_text("5")
    return status("VmRSS")

def peak():
    return max([status("VmHWM"), *closed])

mode, *paths = sys.argv[1:]
paths = [Path(p) for p in paths]
extra = {}
# The encrypted leg (E004-S01-003): the real cipher, a test-only passphrase.
import os
secret = os.environ.get("RSS_TEST_PASSPHRASE")
if secret is not None:
    from l3io.wp.backup.adapters.crypto import PassphraseCipher

    # Each cipher call's KDF window is left out of the peak. age's scrypt runs
    # inside the call before its first write (the header, or the first
    # plaintext chunk) and is freed by then, at a peak of 256 or 512 MiB that
    # varies from call to call. So the peak so far is kept in ``closed`` on
    # entry, and the high-water mark is reset to the current RSS at the first
    # write; what is still resident then (a buffered payload) stays counted.
    kdf_windows = []
    streamed = [0]

    class _AfterKdf:
        def __init__(self, inner):
            self._inner, self._armed = inner, True

        def write(self, data):
            if self._armed:
                self._armed = False
                kdf_windows.append(status("VmHWM") - status("VmRSS"))
                Path("/proc/self/clear_refs").write_text("5")
            streamed[0] += len(data)
            return self._inner.write(data)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _KdfExcluded:
        def __init__(self, cipher):
            self._cipher = cipher

        def check(self):
            self._cipher.check()

        def required_tools(self):
            return self._cipher.required_tools()

        def encrypt(self, reader, writer):
            closed.append(status("VmHWM"))
            self._cipher.encrypt(reader, _AfterKdf(writer))

        def decrypt(self, reader, writer):
            closed.append(status("VmHWM"))
            self._cipher.decrypt(reader, _AfterKdf(writer))

    real_cipher = _KdfExcluded(PassphraseCipher(secret))

if mode in ("backup", "backup-buffered", "backup-noop"):
    from datetime import UTC, datetime
    from pathlib import PurePosixPath
    from l3io.wp.backup.adapters.database import locate_wp_config
    from l3io.wp.backup.adapters.store import LocalArchiveStore
    from l3io.wp.backup.adapters.noop import (
        ActionLog, NoopArchiveStore, NoopArchiveWriter, NoopDatabase,
    )
    from l3io.wp.backup.adapters.tar import TarArchiveWriter, verify_or_raise
    from l3io.wp.backup.adapters.workspace import run_workspace
    from l3io.wp.backup.cli.backup import execute_backup_plan
    from l3io.wp.backup.core.backup import build_backup_plan
    from l3io.wp.backup.domain.backup_scope import BackupScope
    from l3io.wp.backup.domain.database import DumpResult
    from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
    from l3io.wp.backup.domain.site import SiteDescriptor
    from l3io.wp.backup.domain.storage_target import StorageTarget

    site_root, dump_source, final = paths
    if mode == "backup-buffered":
        # The planted regression: tarfile's copy reads its source whole.
        import tarfile as _tarfile

        def _whole(src, dst, length=None, exception=OSError, bufsize=None):
            whole = memoryview(src.read(length))
            for start in range(0, len(whole), 1 << 20):
                dst.write(whole[start : start + (1 << 20)])

        _tarfile.copyfileobj = _whole

    class CopyingDatabase:
        def dump_to(self, location, destination):
            with dump_source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, 256 * 1024)
            return DumpResult(bytes=destination.stat().st_size, duration_ms=0.0)

    site = SiteDescriptor(
        site_root=PurePosixPath(site_root), db_host="db.invalid", db_port=3306,
        db_name="wptest", table_prefix="wp_", site_url=None, home_url=None,
        credential_source="wp-config.php",
    )
    target = StorageTarget(scheme="file", path=PurePosixPath(final))
    plan = build_backup_plan(
        site, BackupScope.ALL, target, encrypted=secret is not None,
        exclusion_source="none", exclusion_patterns=(),
    )
    config = locate_wp_config(site_root, stated=None)
    if mode == "backup-noop":
        # The Dry Run's command halves over the same fixture (AD-4).
        log = ActionLog()
        database, store, writer = (
            NoopDatabase(log), NoopArchiveStore(log), NoopArchiveWriter(log),
        )
    else:
        database = CopyingDatabase()
        store = LocalArchiveStore(verify=verify_or_raise)
        writer = TarArchiveWriter()
    before = baseline()
    with run_workspace(final.parent) as workspace:
        result = execute_backup_plan(
            plan, site=site, config=config, workspace=workspace,
            database=database, store=store, writer=writer,
            cipher=None if secret is None else real_cipher,
            exclusion=INCLUDE_EVERYTHING, origin=None,
            now=datetime.now(UTC),
        )
    growth = peak() - before
    if mode == "backup-noop":
        print(json.dumps({
            "growth": growth, "performed": result.performed,
            "skipped": len(log.calls), "archive_exists": final.exists(),
        }))
        raise SystemExit(0)
    import tarfile
    from l3io.wp.backup.domain.archive_format import MANIFEST_MEMBER, manifest_from_json
    with tarfile.open(final, mode="r|*") as outer:
        manifest = manifest_from_json(
            outer.extractfile(outer.next()).read().decode("utf-8")
        )
    extra = {
        "indexed_bytes": sum(m.size for m in manifest.members),
        "payload_bytes": manifest.payload.size,
        "archive_bytes": final.stat().st_size,
    }
elif mode == "restore":
    import dataclasses
    from pathlib import PurePosixPath
    from l3io.wp.backup.adapters.tar import (
        TarArchiveProbe, decrypt_payload, extract_archive, read_inner_manifest,
    )
    from l3io.wp.backup.domain.archive_format import (
        MemberRole, check_inner_manifest, classify,
    )
    from l3io.wp.backup.domain.storage_target import StorageTarget

    archive, run, destination = paths

    class PassThrough:
        def check(self):
            pass

        def encrypt(self, reader, writer):
            shutil.copyfileobj(reader, writer, 256 * 1024)

        decrypt = encrypt

    probe = TarArchiveProbe()
    target = StorageTarget(scheme="file", path=PurePosixPath(archive))
    before = baseline()
    index = probe.member_index(target)
    view = classify(index)
    if secret is None:
        sealed = dataclasses.replace(view, encrypted=True)
        cipher = PassThrough()
    else:
        sealed, cipher = view, real_cipher
    plain = decrypt_payload(
        target, sealed, cipher=cipher, destination=run / "plain.tar.gz"
    )
    matched = False
    if secret is not None:
        # The inner-Manifest reader streams the whole decrypted payload.
        inner = read_inner_manifest(plain)
        matched = check_inner_manifest(index.manifest_text, inner) is None
    result = extract_archive(
        target, destination, view, roles=frozenset({MemberRole.SITE_ROOT}),
        payload=plain,
    )
    config = probe.read_member(
        target, view, view.members[-1].path, max_bytes=1 << 20,
        payload=None if secret is None else plain,
    )
    growth = peak() - before
    extra = {
        "plain_bytes": plain.stat().st_size,
        "extracted_bytes": result.bytes,
        "read_bytes": len(config),
        "last_member": view.members[-1].path.endswith("wp-config.php"),
        "inner_matches": matched,
    }
elif mode == "verify-v0":
    from l3io.wp.backup.adapters.tar import verify_archive

    (archive,) = paths
    before = baseline()
    result = verify_archive(archive)
    growth = peak() - before
    extra = {"passed": result.passed, "v0": result.format_version is None}
elif mode == "copy":
    source, copy = paths
    before = baseline()
    with source.open("rb") as src, copy.open("wb") as dst:
        shutil.copyfileobj(src, dst, 256 * 1024)
    growth = peak() - before
elif mode == "read":
    (source,) = paths
    before = baseline()
    buffered = source.read_bytes()
    growth = peak() - before
    extra = {"read": len(buffered)}
else:
    raise SystemExit(f"unknown mode {mode!r}")

if secret is not None:
    extra |= {"kdf_windows": len(kdf_windows), "streamed_bytes": streamed[0]}
print(json.dumps({"growth": growth, **extra}))
"""


def _measure(mode: str, *paths: Path, passphrase: str | None = None) -> dict[str, int]:
    """Run ``mode`` in a fresh interpreter; its peak RSS growth and extras.

    ``passphrase`` selects the encrypted leg: the child encrypts with the real
    cipher and reads back through ``read_inner_manifest``.
    """
    env = dict(os.environ)
    env.pop("RSS_TEST_PASSPHRASE", None)
    if passphrase is not None:
        env["RSS_TEST_PASSPHRASE"] = passphrase
    completed = subprocess.run(  # noqa: S603 - this interpreter, a fixed program
        [sys.executable, "-c", _CHILD, mode, *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    result: dict[str, int] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def _fill(path: Path, size: int, *, random: bool = False) -> Path:
    """``size`` bytes, written a MiB at a time; ``random`` is incompressible."""
    block = b"\xa5" * MIB
    with path.open("wb") as handle:
        for _ in range(size // MIB):
            handle.write(os.urandom(MIB) if random else block)
    return path


def test_the_ports_directory_exists_and_has_modules() -> None:
    """Scope assertion for ``test_port_types.py``'s half of AD-21.

    PORTS is built from separate quoted path segments, which a
    search-and-replace over ``l3io/wp`` cannot reach. A namespace move would
    leave it pointing at a directory that does not exist, and a check over an
    absent directory finds nothing to complain about and passes.
    """
    assert PORTS.is_dir(), f"AD-21 scope missing: {PORTS} does not exist"
    assert list(PORTS.glob("*.py")), f"AD-21 scope empty: no modules under {PORTS}"


@linux_proc
def test_a_chunked_copy_stays_under_the_bound(tmp_path: Path) -> None:
    source = _fill(tmp_path / "fixture.bin", FIXTURE_BYTES)
    growth = _measure("copy", source, tmp_path / "copy.bin")["growth"]
    assert growth < RSS_BOUND_BYTES, (
        f"AD-21 — a {FIXTURE_BYTES // MIB} MiB chunked copy grew peak RSS by "
        f"{growth // MIB} MiB, over the {RSS_BOUND_BYTES // MIB} MiB bound."
    )


@linux_proc
def test_the_instrument_detects_a_whole_file_read(tmp_path: Path) -> None:
    """The teeth. Without this, the bound could pass on a broken meter."""
    source = _fill(tmp_path / "fixture.bin", FIXTURE_BYTES)
    measured = _measure("read", source)
    assert measured["read"] == FIXTURE_BYTES
    assert measured["growth"] > RSS_TEETH_BYTES, (
        f"reading {FIXTURE_BYTES // MIB} MiB into memory grew peak RSS by only "
        f"{measured['growth'] // MIB} MiB — the RSS measurement is not measuring, "
        "so the bound in this file proves nothing."
    )


def test_the_fixture_is_ten_times_the_bound() -> None:
    assert SITE_FILE_BYTES + DUMP_BYTES >= 10 * RSS_BOUND_BYTES


def _site(tmp_path: Path, site_bytes: int, dump_bytes: int) -> tuple[Path, ...]:
    site_root = tmp_path / "site"
    site_root.mkdir()
    (site_root / "wp-config.php").write_text("<?php\n", encoding="utf-8")
    _fill(site_root / "large.bin", site_bytes, random=True)
    dump = _fill(tmp_path / "dump.sql", dump_bytes, random=True)
    out = tmp_path / "out"
    out.mkdir()
    return site_root, dump, out / "site.tar.gz"


@linux_proc
@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
def test_the_real_backup_and_restore_paths_stay_under_the_bound(
    tmp_path: Path, encrypted: bool
) -> None:
    """AD-21 with encryption off and on (AG-13): one test over an ``encrypted`` axis."""
    passphrase = f"rss-pp-{uuid.uuid4().hex}" if encrypted else None
    # Encrypted, the child leaves each cipher call's KDF window out of the peak
    # (scrypt, 256 or 512 MiB per call whatever the Archive's size, freed before
    # the call's first write) and measures the rest of the run directly against
    # the bound. A payload held in memory is still resident after the window,
    # so it still breaches. The window must be the cipher's only one, and the
    # payload must stream through the measured part of the call.
    site_root, dump, final = _site(tmp_path, SITE_FILE_BYTES, DUMP_BYTES)

    # The Dry Run's no-op command halves (``noop.py``) over the same fixture:
    # nothing is read, written or published, and nothing accumulates.
    rehearsed = _measure("backup-noop", site_root, dump, final)
    assert rehearsed["performed"] is False
    assert rehearsed["archive_exists"] is False
    assert rehearsed["skipped"] >= 3
    assert rehearsed["growth"] < RSS_BOUND_BYTES

    measured = _measure("backup", site_root, dump, final, passphrase=passphrase)

    # Scope: the whole fixture went in, and an Archive as large came out, so
    # the payload write, the Archive copy and the verifier each moved it.
    floor = 10 * RSS_BOUND_BYTES
    assert measured["indexed_bytes"] >= SITE_FILE_BYTES + DUMP_BYTES >= floor
    assert measured["payload_bytes"] >= floor
    assert measured["archive_bytes"] >= floor
    if encrypted:
        assert measured["kdf_windows"] == 1
        assert measured["streamed_bytes"] >= floor
    grown = measured["growth"]
    assert grown < RSS_BOUND_BYTES, (
        f"AD-21 — backing up {measured['indexed_bytes'] // MIB} MiB grew peak RSS "
        f"by {grown // MIB} MiB outside the KDF window, over the "
        f"{RSS_BOUND_BYTES // MIB} MiB bound."
    )

    # Read it back; the inputs go first so the disk holds one copy fewer.
    shutil.rmtree(site_root)
    dump.unlink()
    run, destination = tmp_path / "run", tmp_path / "restored"
    run.mkdir()
    destination.mkdir()
    restored = _measure("restore", final, run, destination, passphrase=passphrase)
    assert restored["inner_matches"] is encrypted
    assert restored["plain_bytes"] >= floor
    assert restored["extracted_bytes"] >= SITE_FILE_BYTES
    assert restored["last_member"]
    assert restored["read_bytes"] > 0
    if encrypted:
        assert restored["kdf_windows"] == 1
        assert restored["streamed_bytes"] >= floor
    grown = restored["growth"]
    assert grown < RSS_BOUND_BYTES, (
        f"AD-21 — reading back {restored['plain_bytes'] // MIB} MiB grew peak RSS "
        f"by {grown // MIB} MiB outside the KDF window, over the "
        f"{RSS_BOUND_BYTES // MIB} MiB bound."
    )


@linux_proc
def test_verifying_a_large_v0_archive_stays_under_the_bound(tmp_path: Path) -> None:
    """AD-21 for v0 (E004-S01-002): generated at test time, never committed."""
    site_root, dump, final = _site(tmp_path, SITE_FILE_BYTES, DUMP_BYTES)
    archive = make_v0_fixtures.write_v0(final, site_root, dump)
    shutil.rmtree(site_root)
    dump.unlink()
    assert archive.stat().st_size >= 10 * RSS_BOUND_BYTES
    measured = _measure("verify-v0", archive)
    assert (measured["passed"], measured["v0"]) == (True, True)
    assert measured["growth"] < RSS_BOUND_BYTES, (
        f"AD-21 — verifying a {archive.stat().st_size // MIB} MiB v0 Archive grew "
        f"peak RSS by {measured['growth'] // MIB} MiB, over the "
        f"{RSS_BOUND_BYTES // MIB} MiB bound."
    )


@linux_proc
def test_a_buffering_regression_in_the_real_path_breaches_the_bound(
    tmp_path: Path,
) -> None:
    """The teeth on the real path: the same run with a whole-buffer copy."""
    paths = _site(tmp_path, PLANTED_BYTES, MIB)
    measured = _measure("backup-buffered", *paths)
    assert measured["growth"] > RSS_BOUND_BYTES, (
        f"a whole-buffer copy of {PLANTED_BYTES // MIB} MiB inside the backup grew "
        f"peak RSS by only {measured['growth'] // MIB} MiB — the RSS test does not "
        "see the path it claims to cover."
    )


# --------------------------------------------------------------------------
# Reach: every byte mover under adapters/ is one the RSS test drives
# --------------------------------------------------------------------------


def _moves_archive_bytes(module: Path) -> list[str]:
    """Where ``module`` can move Archive bytes, as ``path:line`` strings."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported = [node.module]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in STORE_COMMAND_MEMBERS:
                hits.append(f"{module}:{node.lineno}")
            continue
        else:
            continue
        if any(name.split(".")[0] in ARCHIVE_LIBRARIES for name in imported):
            hits.append(f"{module}:{node.lineno}")
    return hits


def _top_level_names(module: Path) -> frozenset[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return frozenset(node.name for node in tree.body if isinstance(node, kinds))


def _uncovered_byte_movers(
    adapters: Path, covered: frozenset[str] = frozenset(RSS_COVERED)
) -> list[str]:
    return [
        hit
        for path in sorted(adapters.rglob("*.py"))
        if path.relative_to(adapters).as_posix() not in covered
        for hit in _moves_archive_bytes(path)
    ]


def test_the_store_command_members_are_derived() -> None:
    assert "stage" in STORE_COMMAND_MEMBERS


def test_covered_modules_hold_only_what_the_rss_test_drives() -> None:
    """Scope of the coverage itself: a covered module's surface is pinned."""
    for name, expected in RSS_COVERED.items():
        actual = _top_level_names(ADAPTERS / name)
        assert actual == expected, (
            f"AD-21 — {name} gained {sorted(actual - expected)} / lost "
            f"{sorted(expected - actual)}. New code in a covered byte mover needs "
            "its RSS bound in this file before it joins RSS_COVERED."
        )


def test_a_new_function_in_a_covered_module_is_seen(tmp_path: Path) -> None:
    module = tmp_path / "tar.py"
    module.write_text(
        "def write_archive(): ...\ndef restore_archive(): ...\n", encoding="utf-8"
    )
    assert _top_level_names(module) - RSS_COVERED["tar.py"] == {"restore_archive"}


def test_every_covered_module_exists_and_moves_bytes() -> None:
    """A stale or wrong entry in RSS_COVERED fails rather than hiding a module."""
    for name in RSS_COVERED:
        module = ADAPTERS / name
        assert module.is_file(), f"RSS_COVERED names a missing module: {module}"
        assert _moves_archive_bytes(module), f"{module} moves no Archive bytes"


@pytest.mark.parametrize(
    "body",
    [
        "import tarfile\n",
        "import gzip as gz\n",
        "import os, tarfile\n",
        "from tarfile import TarFile\n",
        "from gzip import GzipFile as G\n",
        "def f():\n    import tarfile\n",
        "class Store:\n    def stage(self, target): ...\n",
        "class Store:\n    async def stage(self, target): ...\n",
    ],
)
def test_the_tripwire_sees_an_uncovered_byte_mover_in_any_adapter_module(
    tmp_path: Path, body: str
) -> None:
    """Scope attack: nested, no ``__init__``, and even named like a covered one."""
    adapters = tmp_path / "adapters"
    (adapters / "nested").mkdir(parents=True)
    (adapters / "tar.py").write_text("import tarfile\n", encoding="utf-8")
    (adapters / "nested" / "archive.py").write_text(body, encoding="utf-8")
    (adapters / "nested" / "store.py").write_text(body, encoding="utf-8")
    hits = _uncovered_byte_movers(adapters)
    assert hits, body
    assert all("/nested/" in hit for hit in hits)
    assert any("nested/store.py:" in hit for hit in hits)


@pytest.mark.parametrize(
    "body",
    [
        "import json\n",
        "from . import tarfile\n",
        "def stage_files(): ...\n",
        "def write_archives(): ...\n",
    ],
)
def test_the_tripwire_ignores_unrelated_code(tmp_path: Path, body: str) -> None:
    (tmp_path / "archive.py").write_text(body, encoding="utf-8")
    assert _uncovered_byte_movers(tmp_path) == []


def test_no_adapter_moves_archive_bytes_outside_the_rss_test() -> None:
    assert ADAPTERS.is_dir(), f"AD-21 scope missing: {ADAPTERS}"
    assert list(ADAPTERS.rglob("*.py")), f"AD-21 scope empty: {ADAPTERS}"
    hits = _uncovered_byte_movers(ADAPTERS)
    assert not hits, (
        f"AD-21 — {hits} can move Archive bytes but the RSS test does not drive "
        "it. Add an RSS bound over that path and name the module in RSS_COVERED."
    )
