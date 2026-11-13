"""AD-6/AD-21 — the Archive writer and the Site Root traversal (ADR-0017, ADR-0024).

Tar and gzip are stdlib :mod:`tarfile`, checksums stdlib :mod:`hashlib`; no tar,
gzip or hashing code is written here. The layout is the one
``domain/archive_format.py`` owns, and every member name comes from it.

**Traversal.** :func:`collect_site_root` walks the Site Root with
:func:`os.scandir`, never following a symlink, in sorted pre-order (a directory
before its contents). The exclusion policy decides each entry once, before
anything below it is visited; an excluded directory is pruned unlisted and only
the top-most pruned path is recorded. ``wp-config.php`` at the Site Root is
never excluded. Each directory is opened ``O_NOFOLLOW`` and listed through its
descriptor only after its inode is checked against the entry seen in its
parent, so a directory swapped for a symlink mid-walk fails the backup rather
than leading it outside the tree.

**Writing.** :func:`write_archive` works in three steps: the gzip payload into
the workspace (a :class:`gzip.GzipFile` at the Run's ``compression_level``,
6 unless set, around a ``w|`` tar; owner decision 88: ``tarfile``'s own
``w|gz`` always deflates at 9 and, on Python 3.11, takes no level), then
(with a cipher) the encrypted payload beside it, then the outer tar into
``out`` — the Manifest first, then the payload — inside a gzip stream at
compression level :data:`OUTER_COMPRESSLEVEL` (0: stored blocks), so
the ``.tar.gz`` name is true and ``tar xzf`` reads it, while nothing is
compressed twice (owner decision 21, BL-E002-032; ADR-0024 amendment).
Each copy is a fixed-size chunked stream, so memory is bounded independent of
file and Archive sizes (AD-21); it is linear in the number of entries, which
the Manifest's member index lists. Every member is re-opened by path and
refused unless it is the same inode (device and inode number) the traversal
recorded, so nothing swapped in after the walk — through a renamed parent
directory included — is written. Workspace disk use peaks at the encryption step:
the Dump, the plain payload and the encrypted payload — the Dump plus about two
compressed payloads. Each payload file is deleted once copied.

**Reading (AD-11, AD-23, ADR-0024).** An Archive is untrusted input.
:class:`TarArchiveProbe` streams the outermost tar once (``r|``; gzip, detected
from the first bytes, through :class:`gzip.GzipFile`, whose CRC and length
check is what makes a cut gzip stream visible), hashing each file member in
fixed-size chunks; it never writes a member, follows a link or extracts.
:func:`verify_archive` composes it with ``domain.archive_format.classify`` and
``core.verify``; :func:`verify_or_raise` is the publish gate every backup root
binds into ``LocalArchiveStore``. A plain tar counts as read to its end only
when the end-of-archive marker (two zero blocks) is present, nothing but zeros
follows, and the stream is a whole number of tar records; a stream shorter
than one header is truncated when its bytes could begin a header (a printable
name), else unrecognised.

:func:`extract_archive` is the one extraction function (AD-11). Its filter
callable refuses any hard link, then strips the role prefix, then calls
:func:`tarfile.data_filter` — a symlink it refuses for an absolute or escaping
target is skipped and reported, every other refusal refuses the Archive — and
re-checks containment. :func:`decrypt_payload` is the one place a payload is
decrypted; :meth:`TarArchiveProbe.read_member` the one single-member read.
Each reads a v1 Archive's outer tar through :func:`_outer_tar`, which accepts
both encodings a v1 Archive has had: gzip-wrapped (written since owner decision
21) and plain (every earlier v1 Archive, the committed fixtures included).
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import hashlib
import io
import os
import posixpath
import stat
import tarfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, BinaryIO, Final, Literal, cast

import structlog

from l3io.wp.backup.adapters.access import archive_open_error
from l3io.wp.backup.adapters.phase import PhaseKey, PhaseRecord, join_phase, phase
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.core.verify import verify_probe
from l3io.wp.backup.domain.archive_format import (
    CONFIG_RELATIVE,
    DUMP_RELATIVE,
    ENCRYPTED_PAYLOAD_MEMBER,
    INNER_MANIFEST_MEMBER,
    MANIFEST_MAX_BYTES,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ArchiveView,
    ConfigLocation,
    ExtractResult,
    Integrity,
    Manifest,
    ManifestMember,
    ManifestPayload,
    MemberDigest,
    MemberIndex,
    MemberKind,
    MemberRole,
    SkippedLink,
    TarMemberType,
    VerificationFailure,
    VerificationReason,
    VerificationResult,
    format_label,
    inner_manifest_bytes,
    manifest_size,
    manifest_to_json,
    member_path,
    member_relative,
    parse_created_at,
    pending_payload,
)
from l3io.wp.backup.domain.compression import (
    DEFAULT_COMPRESSION_LEVEL,
    check_compression_level,
)
from l3io.wp.backup.domain.errors import (
    ArchiveReadError,
    ArchiveWriteError,
    BackupError,
    ExtractionRefusedError,
    ExtractionWriteError,
    ManifestTooLargeError,
    MemberReadRefusedError,
    PassphraseRejectedError,
    PayloadDamagedError,
    RunTerminated,
    VerificationFailedError,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.domain.storage_target import StorageTarget, parse_storage_target
from l3io.wp.backup.ports.archive import ManifestBuilder
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__ = [
    "OUTER_COMPRESSLEVEL",
    "ArchiveMemberSource",
    "SiteRootCollection",
    "TarArchiveProbe",
    "TarArchiveWriter",
    "collect_site_root",
    "decrypt_payload",
    "extract_archive",
    "file_member",
    "read_inner_manifest",
    "read_leading_manifest",
    "require_patched_tarfile",
    "verify_archive",
    "verify_archive_manifest",
    "verify_archive_view",
    "verify_or_raise",
    "write_archive",
]

_log = structlog.get_logger(__name__)

#: The Site Root's own config, which no exclusion removes.
_CONFIG_NAME: Final = ConfigLocation.SITE_ROOT.value
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_STAGED_MODE: Final = 0o600
#: Inner members are written role by role, the Dump first (ADR-0024).
_ROLE_ORDER: Final = {MemberRole.DUMP: 0, MemberRole.CONFIG: 1, MemberRole.SITE_ROOT: 2}
_CHUNK: Final = 1024 * 1024
_GZIP_MAGIC: Final = b"\x1f\x8b"
#: The outer gzip stream's level: 0, stored blocks. The payload inside is
#: already gzip (or ciphertext), so the wrapper makes ``.tar.gz`` true without
#: compressing twice (owner decision 21). A transport wrapper, not a layout
#: change: ``format_version`` stays 1.
OUTER_COMPRESSLEVEL: Final = 0
_END_OF_ARCHIVE: Final = 2 * tarfile.BLOCKSIZE
#: The longest end-of-archive run a tar writer pads to: the marker plus a record.
_MAX_TRAILER: Final = tarfile.RECORDSIZE + _END_OF_ARCHIVE
_ASCII_PRINTABLE: Final = range(0x20, 0x7F)
#: Errors meaning the stream itself broke: tar framing, gzip, zlib, I/O.
_STREAM_ERRORS: Final = (tarfile.TarError, EOFError, zlib.error, OSError)
#: The most a GNU long-name/long-link or pax header body may declare (RT-2).
#: tarfile reads such a body whole into memory; a real one is a path or a few
#: pax records, never near this.
_MAX_EXTENDED_HEADER: Final = 1024 * 1024
_EXTENDED_HEADER_TYPES: Final = frozenset(
    {
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        b"X",  # a Solaris extended header, parsed as pax
    }
)


class _BoundedTarInfo(tarfile.TarInfo):
    """A ``TarInfo`` that refuses an oversized extended header before reading it.

    Every reader of an Archive's tar uses it (``tarinfo=_BoundedTarInfo``).

    The hook is ``_proc_member``, the dispatch ``tarfile`` documents in its own
    source as the one to override in a subclass ("The entry point is
    _proc_member() which can be overridden in a subclass"). It runs once per
    header block, after the 512-byte header is parsed and before
    ``_proc_gnulong`` or ``_proc_pax`` reads the body, on 3.11 through 3.14, and
    also for the header that follows a GNU or pax header. The public
    ``frombuf`` is not a hook: since the June 2025 dircheck fix, 3.13+
    ``fromtarfile`` calls ``_frombuf`` directly and never calls ``frombuf``.
    ``tests/contracts/test_tar_header_hook.py`` fails if this hook stops being
    reached on the running interpreter.
    """

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.type in _EXTENDED_HEADER_TYPES and self.size > _MAX_EXTENDED_HEADER:
            msg = f"a tar extended header declares over {_MAX_EXTENDED_HEADER} bytes"
            raise tarfile.ReadError(msg)
        # typeshed does not declare the private dispatch delegated to here.
        member: tarfile.TarInfo = super()._proc_member(archive)  # type: ignore[misc]
        return member


@dataclass(frozen=True, slots=True)
class ArchiveMemberSource:
    """One inner member to write: its name and kind, and where it is read from.

    ``size`` is the byte count expected for a ``file`` (``0`` otherwise) and
    ``identity`` the ``(st_dev, st_ino)`` seen when it was found; a member
    whose size or identity differs when it is written fails the backup.
    """

    path: str
    role: MemberRole
    kind: MemberKind
    source: Path
    size: int
    link_target: str | None
    identity: tuple[int, int]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@dataclass(frozen=True, slots=True)
class SiteRootCollection:
    """What the traversal found: the members to write and what was left out."""

    members: tuple[ArchiveMemberSource, ...]
    excluded: tuple[ExcludedEntry, ...]


def file_member(role: MemberRole, relative: str, source: Path) -> ArchiveMemberSource:
    """A ``file`` member read from ``source``, sized and identified as it is now."""
    try:
        info = source.lstat()
    except OSError as exc:
        raise ArchiveWriteError(source, exc.strerror or type(exc).__name__) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ArchiveWriteError(source, "not a regular file")
    return ArchiveMemberSource(
        path=member_path(role, relative),
        role=role,
        kind=MemberKind.FILE,
        source=source,
        size=info.st_size,
        link_target=None,
        identity=_identity(info),
    )


# --------------------------------------------------------------------------
# Traversal
# --------------------------------------------------------------------------


def _is_outward(relative: str, target: str) -> bool:
    """Whether a link at ``relative`` pointing at ``target`` leaves the Site Root.

    Lexical: an absolute target, or a relative one whose ``..`` segments climb
    above the Site Root. A target that leaves only by passing through another
    symlink is not detected; links are stored as found either way.
    """
    if target.startswith("/"):
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
    return resolved == ".." or resolved.startswith("../")


def _decide(
    policy: ExclusionPolicy, site_root: Path, relative: str, *, is_dir: bool
) -> tuple[bool, str | None]:
    try:
        decision = policy.decide(PurePosixPath(relative), is_dir=is_dir)
    except RunTerminated:
        raise  # BL-E002-092: a SIGTERM is not a policy failure
    except Exception as exc:
        reason = (
            f"the exclusion policy failed deciding {relative!r}: {type(exc).__name__}"
        )
        raise ArchiveWriteError(site_root / relative, reason) from exc
    return decision.excluded, decision.pattern


def _scan(
    directory: Path, identity: tuple[int, int]
) -> list[tuple[str, os.stat_result]]:
    """``directory``'s entries and their ``lstat``, sorted by name.

    Refused unless ``directory`` is the inode its parent showed; each entry is
    stat'ed through the directory's descriptor, so the listing is of that inode.
    """
    try:
        fd = os.open(directory, _DIR_FLAGS)
    except OSError as exc:
        raise ArchiveWriteError(directory, exc.strerror or type(exc).__name__) from exc
    try:
        if _identity(os.fstat(fd)) != identity:
            raise ArchiveWriteError(directory, "changed while it was being read")
        with os.scandir(fd) as entries:
            found = [(e.name, e.stat(follow_symlinks=False)) for e in entries]
        return sorted(found, key=lambda item: item[0])
    except OSError as exc:
        raise ArchiveWriteError(directory, exc.strerror or type(exc).__name__) from exc
    finally:
        os.close(fd)


def collect_site_root(site_root: Path, policy: ExclusionPolicy) -> SiteRootCollection:
    """Walk ``site_root`` into ``site-root``-role members (ADR-0017).

    Every directory, the Site Root included, is a ``dir``; a symlink is a
    ``symlink`` holding :func:`os.readlink`'s target, with a WARNING when that
    target is absolute or leaves the Site Root; a multiply-linked file is an
    ordinary ``file`` with its full content, the dropped relationship logged
    once per inode; sockets, FIFOs and devices are skipped and logged.
    """
    try:
        root_identity = _identity(site_root.lstat())
    except OSError as exc:
        raise ArchiveWriteError(site_root, exc.strerror or type(exc).__name__) from exc
    members = [
        ArchiveMemberSource(
            path=member_path(MemberRole.SITE_ROOT, ""),
            role=MemberRole.SITE_ROOT,
            kind=MemberKind.DIR,
            source=site_root,
            size=0,
            link_target=None,
            identity=root_identity,
        )
    ]
    excluded: list[ExcludedEntry] = []
    linked: dict[tuple[int, int], list[str]] = {}
    pending: list[tuple[str, Path, str, os.stat_result]] = [
        ("", site_root, entry, info)
        for entry, info in reversed(_scan(site_root, root_identity))
    ]
    while pending:
        parent, parent_path, entry_name, info = pending.pop()
        relative = f"{parent}/{entry_name}" if parent else entry_name
        source = parent_path / entry_name
        is_dir = stat.S_ISDIR(info.st_mode)
        is_excluded, pattern = _decide(policy, site_root, relative, is_dir=is_dir)
        if is_excluded and relative == _CONFIG_NAME:
            _log.debug("wp_config_exclusion_ignored", path=relative)
        elif is_excluded:
            excluded.append(
                ExcludedEntry(path=relative, pattern=pattern or "", is_dir=is_dir)
            )
            continue
        name = member_path(MemberRole.SITE_ROOT, relative)
        identity = _identity(info)
        if stat.S_ISLNK(info.st_mode):
            # Path.readlink normalises the text; the link is stored exactly.
            target = os.readlink(source)  # noqa: PTH115
            if _is_outward(relative, target):
                _log.warning("symlink_outside_site_root", path=relative, target=target)
            members.append(
                ArchiveMemberSource(
                    name,
                    MemberRole.SITE_ROOT,
                    MemberKind.SYMLINK,
                    source,
                    0,
                    target,
                    identity,
                )
            )
        elif is_dir:
            members.append(
                ArchiveMemberSource(
                    name,
                    MemberRole.SITE_ROOT,
                    MemberKind.DIR,
                    source,
                    0,
                    None,
                    identity,
                )
            )
            children = reversed(_scan(source, identity))
            pending.extend((relative, source, *child) for child in children)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink > 1:
                linked.setdefault(identity, []).append(relative)
            members.append(
                ArchiveMemberSource(
                    name,
                    MemberRole.SITE_ROOT,
                    MemberKind.FILE,
                    source,
                    info.st_size,
                    None,
                    identity,
                )
            )
        else:
            _log.warning("special_file_skipped", path=relative)
    for paths in linked.values():
        _log.warning("hard_link_stored_as_file", paths=tuple(paths))
    return SiteRootCollection(members=tuple(members), excluded=tuple(excluded))


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


class _HashingWriter(io.RawIOBase):
    """Counts and SHA-256-hashes the bytes passing through to ``target``."""

    def __init__(self, target: IO[bytes]) -> None:
        super().__init__()
        self._target = target
        self.digest = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        view = memoryview(data)
        self._target.write(view)
        self.digest.update(view)
        self.size += view.nbytes
        return view.nbytes


def _member_changed(member: ArchiveMemberSource) -> ArchiveWriteError:
    reason = f"member {member.path!r} changed while it was being written"
    return ArchiveWriteError(member.source, reason)


def _header(member: ArchiveMemberSource, info: os.stat_result) -> tarfile.TarInfo:
    """A regular ``TarInfo`` — never ``gettarinfo``'s hard-link tracking."""
    header = tarfile.TarInfo(member.path)
    header.mode = stat.S_IMODE(info.st_mode)
    header.mtime = info.st_mtime
    header.uid = info.st_uid
    header.gid = info.st_gid
    if member.kind is MemberKind.FILE:
        header.type = tarfile.REGTYPE
        header.size = member.size
    elif member.kind is MemberKind.DIR:
        header.type = tarfile.DIRTYPE
    else:
        header.type = tarfile.SYMTYPE
        header.linkname = member.link_target or ""
    return header


def _add_file(inner: tarfile.TarFile, member: ArchiveMemberSource) -> None:
    # O_NONBLOCK: a FIFO swapped in after the walk would block a plain open
    # until a writer appears (BL-E002-062); non-blocking, the open returns and
    # the type check below refuses it. Cleared before any read.
    try:
        fd = os.open(member.source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise _member_changed(member) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != member.size
            or _identity(info) != member.identity
        ):
            raise _member_changed(member)
        os.set_blocking(handle.fileno(), True)
        try:
            inner.addfile(_header(member, info), handle)
        except OSError as exc:
            if os.fstat(handle.fileno()).st_size != member.size:
                raise _member_changed(member) from exc
            raise
        if handle.read(1):
            raise _member_changed(member)


def _add(inner: tarfile.TarFile, member: ArchiveMemberSource) -> ManifestMember:
    if member.kind is MemberKind.FILE:
        _add_file(inner, member)
    else:
        try:
            info = member.source.lstat()
        except OSError as exc:
            raise _member_changed(member) from exc
        if _identity(info) != member.identity:
            raise _member_changed(member)
        inner.addfile(_header(member, info))
    return ManifestMember(
        path=member.path,
        role=member.role,
        kind=member.kind,
        size=member.size if member.kind is MemberKind.FILE else 0,
        link_target=member.link_target,
    )


def _add_inner_manifest(inner: tarfile.TarFile, sealed: tuple[bytes, float]) -> None:
    text, mtime = sealed
    header = tarfile.TarInfo(INNER_MANIFEST_MEMBER)
    header.size = len(text)
    header.mode = _STAGED_MODE
    header.mtime = mtime
    inner.addfile(header, io.BytesIO(text))


def _write_payload(
    destination: Path,
    members: Iterable[ArchiveMemberSource],
    *,
    seal: Callable[[tuple[ManifestMember, ...]], tuple[bytes, float]] | None,
    compression_level: int,
) -> tuple[tuple[ManifestMember, ...], _HashingWriter]:
    """Write the gzip inner tar; ``seal`` gives the inner Manifest and its mtime.

    The inner Manifest is written last. The gzip stream is deflated at
    ``compression_level`` (owner decision 88); its header names no path and
    carries the time it is written, as ``tarfile``'s ``w|gz`` header did.
    """
    ordered = sorted(members, key=lambda member: _ROLE_ORDER[member.role])
    with destination.open("xb") as raw:
        hashed = _HashingWriter(raw)
        with (
            gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=cast("IO[bytes]", hashed),
                compresslevel=compression_level,
            ) as compressed,
            tarfile.open(fileobj=cast("IO[bytes]", compressed), mode="w|") as inner,
        ):
            index = tuple(_add(inner, member) for member in ordered)
            if seal is not None:
                _add_inner_manifest(inner, seal(index))
    return index, hashed


def _encrypt(cipher: PayloadCipher, plain: Path, destination: Path) -> _HashingWriter:
    # The one cipher is age with a passphrase (ADR-0024; E004-S01-003).
    _log.info("encryption_configured", scheme="age", mode="passphrase")
    with phase(BackupStep.ENCRYPT.value) as record:
        with plain.open("rb") as reader, destination.open("xb") as raw:
            hashed = _HashingWriter(raw)
            cipher.encrypt(reader, cast("IO[bytes]", hashed))
        record.bytes = hashed.size
    plain.unlink()
    return hashed


def _outer_header(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    header = tarfile.TarInfo(name)
    header.size = size
    header.mode = _STAGED_MODE
    header.mtime = mtime
    return header


def _write_outer(out: IO[bytes], manifest: Manifest, payload: Path) -> None:
    text = manifest_to_json(manifest).encode("utf-8")
    # The Run clock's time, as the Manifest records it (SD-01-5).
    mtime = parse_created_at(manifest.created_at).timestamp()
    with (
        # filename="": the gzip header names no path; mtime: the Run clock's.
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=out,
            compresslevel=OUTER_COMPRESSLEVEL,
            mtime=int(mtime),
        ) as wrapped,
        tarfile.open(fileobj=cast("IO[bytes]", wrapped), mode="w|") as outer,
    ):
        manifest_header = _outer_header(MANIFEST_MEMBER, len(text), mtime)
        outer.addfile(manifest_header, io.BytesIO(text))
        with payload.open("rb") as source:
            header = _outer_header(manifest.payload.name, manifest.payload.size, mtime)
            outer.addfile(header, source)
    payload.unlink()


def write_archive(
    out: IO[bytes],
    manifest_builder: Callable[[tuple[ManifestMember, ...]], Manifest],
    members: Iterable[ArchiveMemberSource],
    *,
    workspace: Path,
    cipher: PayloadCipher | None,
    compression_level: int = DEFAULT_COMPRESSION_LEVEL,
) -> Manifest:
    """Write one v1 Archive into ``out`` and return its Manifest (ADR-0024).

    ``manifest_builder`` is called once, with the member index computed here,
    and returns the Manifest; ``encrypted`` and ``payload`` are then set from
    what was written. The outer Manifest never passes through the cipher.
    With a cipher, the plain payload ends with the inner Manifest
    (:func:`~l3io.wp.backup.domain.archive_format.inner_manifest_bytes`; the
    ADR-0024 amendment), which the outer one then equals less the encrypted
    payload's size and sha256. Any failure removes the payload files from
    ``workspace``; an ``OSError`` or ``tarfile`` error is re-raised as
    :class:`ArchiveWriteError`. The payload is deflated at
    ``compression_level`` before it is encrypted; a level outside 1-9 is
    :class:`UsageError` before anything is written (owner decision 88).
    """
    check_compression_level(compression_level)
    plain = workspace / PAYLOAD_MEMBER
    sealed = workspace / ENCRYPTED_PAYLOAD_MEMBER
    writing = plain
    built: list[Manifest] = []

    def seal(index: tuple[ManifestMember, ...]) -> tuple[bytes, float]:
        pending = dataclasses.replace(
            manifest_builder(index),
            encrypted=True,
            payload=pending_payload(encrypted=True),
        )
        built.append(pending)
        stamp = parse_created_at(pending.created_at).timestamp()
        return inner_manifest_bytes(pending), stamp

    try:
        index, hashed = _write_payload(
            plain,
            members,
            seal=None if cipher is None else seal,
            compression_level=compression_level,
        )
        payload, name = plain, PAYLOAD_MEMBER
        if cipher is not None:
            writing = sealed
            hashed = _encrypt(cipher, plain, sealed)
            payload, name = sealed, ENCRYPTED_PAYLOAD_MEMBER
        manifest = dataclasses.replace(
            built[0] if built else manifest_builder(index),
            encrypted=cipher is not None,
            payload=ManifestPayload(
                name=name, size=hashed.size, sha256=hashed.digest.hexdigest()
            ),
        )
        writing = payload
        _write_outer(out, manifest, payload)
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ArchiveWriteError(writing, _reason(exc)) from exc
    finally:
        plain.unlink(missing_ok=True)
        sealed.unlink(missing_ok=True)
    return manifest


#: Upper bounds for the payload fields only known once it is written.
_LARGEST_SIZE: Final = 2**63 - 1
_A_SHA256: Final = "f" * 64


def _refuse_oversized_manifest(
    build: Callable[[tuple[ManifestMember, ...]], Manifest],
    members: Sequence[ArchiveMemberSource],
    *,
    encrypted: bool,
) -> None:
    """BL-E004-053: ``ManifestTooLargeError`` before anything is written.

    The Manifest is built from the collected members with the same builder
    and measured with the same serialiser; only the payload's size and
    sha256 are not known yet, so their longest forms stand in: the size is an
    upper bound, never an estimate by hand.
    """
    index = tuple(
        ManifestMember(
            path=m.path,
            role=m.role,
            kind=m.kind,
            size=m.size if m.kind is MemberKind.FILE else 0,
            link_target=m.link_target,
        )
        for m in members
    )
    name = ENCRYPTED_PAYLOAD_MEMBER if encrypted else PAYLOAD_MEMBER
    bound = dataclasses.replace(
        build(index),
        encrypted=encrypted,
        payload=ManifestPayload(name=name, size=_LARGEST_SIZE, sha256=_A_SHA256),
    )
    size = manifest_size(bound)
    if size > MANIFEST_MAX_BYTES:
        raise ManifestTooLargeError(
            members=len(index), size=size, limit=MANIFEST_MAX_BYTES
        )


class TarArchiveWriter:
    """``ArchiveWriterCommand``: collection, encryption and the write (ADR-0019).

    Calls :func:`collect_site_root` and :func:`write_archive` unchanged and
    emits the ``COLLECT_SITE_ROOT``, ``ENCRYPT`` (encrypted path) and
    ``WRITE_ARCHIVE`` phase records. Inner members are the Dump, the config,
    then the Site Root's, in that order.
    """

    __slots__ = ()

    def write(
        self,
        out: IO[bytes],
        *,
        site_root: Path | None,
        dump: Path | None,
        config: Path,
        exclusion: ExclusionPolicy,
        cipher: PayloadCipher | None,
        manifest_builder: ManifestBuilder,
        workspace: Path,
        compression_level: int,
    ) -> Manifest:
        """Collect ``site_root`` (if any) and write the Archive into ``out``."""
        collection: SiteRootCollection | None = None
        if site_root is not None:
            with phase(BackupStep.COLLECT_SITE_ROOT.value) as record:
                collection = collect_site_root(site_root, exclusion)
                record.bytes = sum(m.size for m in collection.members)
        members: list[ArchiveMemberSource] = []
        if dump is not None:
            members.append(file_member(MemberRole.DUMP, DUMP_RELATIVE, dump))
        members.append(file_member(MemberRole.CONFIG, CONFIG_RELATIVE, config))
        excluded: tuple[ExcludedEntry, ...] = ()
        if collection is not None:
            members.extend(collection.members)
            excluded = collection.excluded

        def build(index: tuple[ManifestMember, ...]) -> Manifest:
            return manifest_builder(index, excluded)

        _refuse_oversized_manifest(build, members, encrypted=cipher is not None)
        with phase(BackupStep.WRITE_ARCHIVE.value) as record:
            manifest = write_archive(
                out,
                build,
                members,
                workspace=workspace,
                cipher=cipher,
                compression_level=compression_level,
            )
            record.bytes = manifest.payload.size
        return manifest


# --------------------------------------------------------------------------
# Reading: opening an Archive
# --------------------------------------------------------------------------


def _archive_path(source: StorageTarget) -> Path:
    """The local path of ``source``; a non-``file:`` scheme is refused."""
    return Path(parse_storage_target(source.scheme, str(source.path)).path)


def _open(path: Path) -> BinaryIO:
    try:
        return cast("BinaryIO", path.open("rb"))
    except OSError as exc:
        raise archive_open_error(path, exc) from exc


def _reason(exc: BaseException) -> str:
    """An ``OSError``'s errno text, else the type's name; never the message."""
    return getattr(exc, "strerror", None) or type(exc).__name__


class _Counter(io.RawIOBase):
    """Counts the bytes read through it and keeps the first tar block of them."""

    def __init__(self, source: IO[bytes]) -> None:
        super().__init__()
        self._source = source
        self.count = 0
        self.head = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview, /) -> int:  # type: ignore[override]
        view = memoryview(buffer).cast("B")
        data = self._source.read(len(view))
        size = len(data)
        view[:size] = data
        if len(self.head) < tarfile.BLOCKSIZE:
            self.head = (self.head + data)[: tarfile.BLOCKSIZE]
        self.count += size
        return size


class _ReadSide(io.RawIOBase):
    """Passes reads through, keeping the ``OSError`` one raised (BL-E002-049).

    A stream that both reads the Archive and writes a destination can then
    tell a read failure (:class:`ArchiveReadError`) from a write failure
    (:class:`ArchiveWriteError`).
    """

    def __init__(self, source: IO[bytes]) -> None:
        super().__init__()
        self._source = source
        self.failed: OSError | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview, /) -> int:  # type: ignore[override]
        view = memoryview(buffer).cast("B")
        try:
            data = self._source.read(len(view))
        except OSError as exc:
            self.failed = exc
            raise
        size = len(data)
        view[:size] = data
        return size


class _WriteSide(io.RawIOBase):
    """Passes writes through, keeping the ``OSError`` one raised (BL-E002-049)."""

    def __init__(self, target: IO[bytes]) -> None:
        super().__init__()
        self._target = target
        self.failed: OSError | None = None

    def writable(self) -> bool:
        return True

    def write(self, data: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        try:
            return self._target.write(data)
        except OSError as exc:
            self.failed = exc
            raise

    def flush(self) -> None:
        """Flush the target too, so its buffered write errors are kept here."""
        if self._target.closed:  # this wrapper's own close, after the target's
            return
        try:
            self._target.flush()
        except OSError as exc:
            self.failed = exc
            raise


# --------------------------------------------------------------------------
# Reading: the probe
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Scan:
    """One streamed pass over the outermost tar."""

    names: list[str] = field(default_factory=list)
    digests: dict[str, MemberDigest] = field(default_factory=dict)
    manifest_text: str | None = None
    complete: bool = False
    gzip: bool = False
    bytes_read: int = 0
    end: int = 0

    def index(self) -> MemberIndex:
        return MemberIndex(
            names=tuple(self.names),
            first_member=self.names[0] if self.names else None,
            manifest_text=self.manifest_text,
            readable_to_end=self.complete,
            gzip=self.gzip,
        )


def _tar_type(info: tarfile.TarInfo) -> TarMemberType:
    if info.islnk():
        return TarMemberType.HARDLINK
    if info.isfile():
        return TarMemberType.FILE
    if info.isdir():
        return TarMemberType.DIR
    if info.issym():
        return TarMemberType.SYMLINK
    return TarMemberType.OTHER


def _digest(
    tar: tarfile.TarFile, info: tarfile.TarInfo, *, keep: bool
) -> tuple[MemberDigest, bytes | None]:
    """Stream one member into SHA-256; with ``keep``, its bytes too."""
    kind = _tar_type(info)
    link = (
        info.linkname
        if kind in {TarMemberType.SYMLINK, TarMemberType.HARDLINK}
        else None
    )
    if kind is not TarMemberType.FILE:
        return MemberDigest(
            tar_type=kind, size=info.size, sha256=None, link_name=link
        ), None
    handle = tar.extractfile(info)
    if handle is None:  # pragma: no cover - isfile() guarantees a handle
        msg = "a regular member has no content"
        raise tarfile.ReadError(msg)
    digest = hashlib.sha256()
    kept = bytearray() if keep else None
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
        if kept is not None:
            kept += chunk
    return (
        MemberDigest(
            tar_type=kind, size=info.size, sha256=digest.hexdigest(), link_name=None
        ),
        None if kept is None else bytes(kept),
    )


def _began_as_a_tar(head: bytes) -> bool:
    """Whether a stream that failed at its first header had begun as a tar.

    A whole first block must parse as a tar header (``TarInfo.frombuf``, its
    checksum included); a shorter stream counts when it is a prefix of the
    gzip magic or its bytes up to the first NUL are a printable name, the way
    every tar header begins.
    """
    if len(head) >= tarfile.BLOCKSIZE:
        try:
            tarfile.TarInfo.frombuf(head, "utf-8", "surrogateescape")
        except tarfile.HeaderError:
            return False
        return True
    if head and _GZIP_MAGIC.startswith(head):
        return True
    name = head.split(b"\x00", 1)[0]
    return bool(name) and all(b in _ASCII_PRINTABLE or b >= 0x80 for b in name)


def _walk(tar: tarfile.TarFile, scan: _Scan) -> None:
    for info in tar:
        first = not scan.names
        scan.names.append(info.name)
        keep = (
            first
            and info.name == MANIFEST_MEMBER
            and info.isfile()
            and info.size <= MANIFEST_MAX_BYTES
        )
        digest, kept = _digest(tar, info, keep=keep)
        held = scan.digests.get(info.name)
        if held is None or digest.size > held.size:
            # A repeated name is sized by its largest copy, so a small first
            # copy cannot hide a huge second one from the space check.
            scan.digests[info.name] = digest
        if kept is not None:
            with contextlib.suppress(UnicodeDecodeError):
                scan.manifest_text = kept.decode("utf-8")
    scan.end = tar.offset
    stream = cast("IO[bytes]", tar.fileobj)
    zeros = True
    while chunk := stream.read(_CHUNK):  # to the end: GzipFile checks its CRC here
        zeros = zeros and chunk.count(0) == len(chunk)
    scan.complete = zeros and stream.tell() - tar.offset >= _END_OF_ARCHIVE


def _clean_trailer(handle: BinaryIO, end: int) -> bool:
    """Whether a plain tar's bytes from ``end`` are its end-of-archive marker.

    tarfile stops silently at an invalid header after the first, having
    consumed it, so the bytes are re-read from the file: at least two zero
    blocks, nothing but zeros, at most :data:`_MAX_TRAILER` of them, and the
    stream a whole number of tar records.
    """
    handle.seek(end)
    trailer = handle.read(_MAX_TRAILER + 1)
    return (
        _END_OF_ARCHIVE <= len(trailer) <= _MAX_TRAILER
        and trailer.count(0) == len(trailer)
        and (end + len(trailer)) % tarfile.RECORDSIZE == 0
    )


def _stream_outer(path: Path) -> _Scan:
    """Stream the outermost tar of ``path`` once. Raises only for a missing file."""
    scan = _Scan()
    with _open(path) as handle:
        raw = _Counter(handle)
        scan.gzip = cast("io.BufferedReader", handle).peek(2)[:2] == _GZIP_MAGIC
        logical = (
            _Counter(cast("IO[bytes]", gzip.GzipFile(fileobj=raw, mode="rb")))
            if scan.gzip
            else raw
        )
        try:
            with tarfile.open(
                fileobj=cast("IO[bytes]", logical), mode="r|", tarinfo=_BoundedTarInfo
            ) as tar:
                _walk(tar, scan)
            if scan.complete and not scan.gzip:
                scan.complete = _clean_trailer(handle, scan.end)
        except _STREAM_ERRORS as exc:
            _log.debug("archive_read_stopped", error=type(exc).__name__)
            # A stream that is not a tar from its first header has no names
            # and nothing more to read: the classifier calls it unrecognised.
            at_first_header = not scan.names and isinstance(exc, tarfile.ReadError)
            scan.complete = at_first_header and not _began_as_a_tar(logical.head)
        scan.bytes_read = raw.count
    return scan


def read_leading_manifest(path: Path) -> str | None:
    """The Manifest text of a v1 Archive at ``path``, reading only its start.

    ``restore --archive latest`` (owner decision 64) reads one Manifest per
    Archive in a directory; the Manifest is the outer tar's first member, so
    nothing past it is read. ``None`` for anything else: a file that is not
    an Archive, a first member that is not a Manifest of at most
    ``MANIFEST_MAX_BYTES``, or a read that fails (never an exception).
    """
    try:
        with path.open("rb") as handle:
            head = handle.peek(2)[:2]
            stream: IO[bytes] = (
                cast("IO[bytes]", gzip.GzipFile(fileobj=handle, mode="rb"))
                if head == _GZIP_MAGIC
                else handle
            )
            with tarfile.open(
                fileobj=stream, mode="r|", tarinfo=_BoundedTarInfo
            ) as tar:
                first = tar.next()
                if (
                    first is None
                    or first.name != MANIFEST_MEMBER
                    or not first.isfile()
                    or first.size > MANIFEST_MAX_BYTES
                ):
                    return None
                body = tar.extractfile(first)
                return None if body is None else body.read().decode("utf-8")
    except (OSError, UnicodeDecodeError, *_STREAM_ERRORS) as exc:
        _log.debug("archive_manifest_unread", path=str(path), error=type(exc).__name__)
        return None


class TarArchiveProbe:
    """``ArchiveProbe`` over local Archives with stdlib ``tarfile`` streams."""

    __slots__ = ()

    def member_index(self, source: StorageTarget) -> MemberIndex:
        """The outer tar's member names and Manifest text, streamed once."""
        return _stream_outer(_archive_path(source)).index()

    def member_digests(self, source: StorageTarget) -> Mapping[str, MemberDigest]:
        """Every outer member's type, size and SHA-256 (largest occurrence)."""
        return MappingProxyType(_stream_outer(_archive_path(source)).digests)

    def read_member(
        self,
        source: StorageTarget,
        view: ArchiveView,
        path: str,
        *,
        max_bytes: int,
        payload: Path | None = None,
    ) -> bytes:
        """One regular-file member's bytes, refusing anything but a small file.

        Reads through the plain payload as :func:`extract_archive` does. Refuses
        (:class:`MemberReadRefusedError`) a path not in the member index, one
        that is not a regular file, one larger than ``max_bytes`` and one whose
        name occurs more than once in the tar (extraction would see the last
        copy, this the first; BL-E002-050), so the stream is read to its end.
        Writes nothing.
        """
        if _is_reserved(path):
            raise MemberReadRefusedError(path, "the reserved inner Manifest name")
        member = next((m for m in view.members if m.path == path), None)
        if member is None:
            raise MemberReadRefusedError(path, "not in the member index")
        if member.kind is not MemberKind.FILE:
            raise MemberReadRefusedError(path, "not a regular file")
        archive = _archive_path(source)
        found: bytes | None = None
        try:
            with _inner(archive, view, payload) as tar:
                for info in tar:
                    if info.name != path:
                        continue
                    if found is not None:
                        raise MemberReadRefusedError(path, "repeated in the archive")
                    if not info.isfile():
                        raise MemberReadRefusedError(path, "not a regular file")
                    if info.size > max_bytes:
                        reason = f"{info.size} bytes is over the {max_bytes}-byte cap"
                        raise MemberReadRefusedError(path, reason)
                    handle = tar.extractfile(info)
                    if handle is None:  # pragma: no cover - isfile() guarantees one
                        raise MemberReadRefusedError(path, "not a regular file")
                    found = handle.read(info.size)
        except _STREAM_ERRORS as exc:
            raise ArchiveReadError(archive, _reason(exc)) from exc
        if found is None:
            raise ArchiveReadError(archive, f"member {path!r} is not in the archive")
        return found


# --------------------------------------------------------------------------
# Reading: verification, the publish gate
# --------------------------------------------------------------------------


def verify_archive(path: Path) -> VerificationResult:
    """Verify the Archive at ``path`` (AD-23, ADR-0024). Writes nothing.

    Annotates the enclosing ``VERIFY`` phase when there is one (the store's
    publish), else emits its own: ``bytes`` read, ``format_version`` (or
    ``"v0"``), ``integrity``, ``dump_present``, ``passed``,
    ``authenticated`` (always ``False``: the checksums live in the
    unauthenticated Manifest, so a keyless pass proves consistency, not
    authenticity) and, on failure, the first ``reason`` and ``member``; the
    format fields are ``None`` when the Archive could not be classified. A
    failed verification marks the phase ``failed`` with
    ``VERIFICATION_FAILED`` though it returns rather than raises. A missing or
    unreadable file raises :class:`ArchiveNotFoundError`; one this user may not
    read, :class:`~l3io.wp.backup.domain.errors.PathNotAccessibleError`.
    """
    return verify_archive_view(path)[1]


def verify_archive_view(
    path: Path, *, phase_key: PhaseKey = BackupStep.VERIFY
) -> tuple[ArchiveView | None, VerificationResult]:
    """:func:`verify_archive`, also returning the classified view (``None`` if none).

    For a caller that acts on the verified Archive without streaming it a
    second time to classify it. ``phase_key`` is the open phase it annotates
    (the restore's is ``RestorePhase.VERIFY``), so a Run emits one
    record for it. :func:`verify_archive_manifest` also returns the Manifest's
    text.
    """
    view, result, _ = verify_archive_manifest(path, phase_key=phase_key)
    return view, result


def verify_archive_manifest(
    path: Path, *, phase_key: PhaseKey = BackupStep.VERIFY
) -> tuple[ArchiveView | None, VerificationResult, str | None]:
    """:func:`verify_archive_view`, also returning the outer Manifest's text.

    The text is the one this single scan verified (``None`` for v0 or an
    unreadable Manifest), so the inner-Manifest check
    (:func:`~l3io.wp.backup.domain.archive_format.check_inner_manifest`)
    compares the Manifest that was verified, from one read (ADR-0024
    amendment). ``verify --authenticate`` and the restore's ``VERIFY`` call it.
    """
    with join_phase(phase_key) as record:
        scan = _stream_outer(path)
        view, result = verify_probe(scan.index(), scan.digests)
        record.bytes = scan.bytes_read
        for field, value in () if view is None else view.unknown_values:
            # E002-S01-001: an unknown value reads as absent, with a WARNING.
            _log.warning("manifest_value_unknown", field=field, value=repr(value))
        # An Archive that was never classified was read as no format at all.
        record.fields.update(
            format_version=None if view is None else format_label(view.format_version),
            integrity=None if view is None else result.integrity.value,
            dump_present=None if view is None else view.dump_present,
            passed=result.passed,
            authenticated=False,
        )
        if result.failures:
            first = result.failures[0]
            record.fields.update(
                reason=first.reason.value,
                member=None if first.member is None else repr(first.member),
            )
        if not result.passed:
            record.fail(VerificationFailedError.exit_code)
    return view, result, scan.manifest_text


def verify_or_raise(path: Path) -> None:
    """The publish gate: raise :class:`VerificationFailedError` unless it passes.

    This package writes only v1 Archives, so the gate also requires
    ``VERIFIED``: a v0-shaped ``STRUCTURAL_ONLY`` pass would be a writer
    defect, and is refused as ``UNRECOGNISED_FORMAT`` (BL-E002-048).
    """
    result = verify_archive(path)
    if result.passed and result.integrity is not Integrity.VERIFIED:
        unrecognised = VerificationFailure(None, VerificationReason.UNRECOGNISED_FORMAT)
        result = VerificationResult(
            passed=False,
            integrity=result.integrity,
            format_version=result.format_version,
            failures=(unrecognised,),
        )
    if not result.passed:
        raise VerificationFailedError(result, str(path))


# --------------------------------------------------------------------------
# Reading: the plain payload, extraction and decryption
# --------------------------------------------------------------------------


def _payload_body(outer: tarfile.TarFile, name: str, archive: Path) -> IO[bytes]:
    for info in outer:
        if info.name == name and info.isfile():
            body = outer.extractfile(info)
            if body is not None:
                return body
    raise ArchiveReadError(archive, f"the payload {name!r} is not in the archive")


@contextlib.contextmanager
def _outer_tar(archive: Path) -> Iterator[tarfile.TarFile]:
    """A v1 Archive's outer tar, streamed (``r|``), in either encoding.

    gzip-wrapped (written since owner decision 21) is detected from the magic
    and read through :class:`gzip.GzipFile`; a plain tar (every earlier v1
    Archive) is read as it is. Both stay readable permanently.
    """
    with contextlib.ExitStack() as stack:
        handle = stack.enter_context(_open(archive))
        body: IO[bytes] = handle
        if cast("io.BufferedReader", handle).peek(2)[:2] == _GZIP_MAGIC:
            body = cast(
                "IO[bytes]",
                stack.enter_context(gzip.GzipFile(fileobj=handle, mode="rb")),
            )
        yield stack.enter_context(
            tarfile.open(fileobj=body, mode="r|", tarinfo=_BoundedTarInfo)
        )


@contextlib.contextmanager
def _inner(
    archive: Path, view: ArchiveView, payload: Path | None
) -> Iterator[tarfile.TarFile]:
    """The tar holding ``view``'s members: v0's Archive, else the plain payload."""
    if view.payload is None:
        with (
            _open(archive) as handle,
            tarfile.open(fileobj=handle, mode="r|*", tarinfo=_BoundedTarInfo) as tar,
        ):
            yield tar
    elif payload is not None:
        try:
            handle = payload.open("rb")
        except OSError as exc:
            # The decrypted payload is read, not written (BL-E002-049).
            raise ArchiveReadError(payload, _reason(exc)) from exc
        with (
            handle,
            tarfile.open(fileobj=handle, mode="r:gz", tarinfo=_BoundedTarInfo) as tar,
        ):
            yield tar
    elif view.encrypted:
        msg = "an encrypted archive needs payload= (the file decrypt_payload wrote)"
        raise ValueError(msg)
    else:
        with _outer_tar(archive) as outer:
            body = _payload_body(outer, view.payload.name, archive)
            with tarfile.open(
                fileobj=body, mode="r|gz", tarinfo=_BoundedTarInfo
            ) as tar:
                yield tar


_SkipReason = Literal["absolute", "outside_destination"]


def _is_reserved(name: str) -> bool:
    """``name`` is the inner Manifest's reserved name, or a path below it."""
    return name == INNER_MANIFEST_MEMBER or name.startswith(f"{INNER_MANIFEST_MEMBER}/")


def _header_kind(member: tarfile.TarInfo) -> MemberKind | None:
    if member.isfile():
        return MemberKind.FILE
    if member.isdir():
        return MemberKind.DIR
    return MemberKind.SYMLINK if member.issym() else None


def _existing_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of what ``path`` names, or ``None`` if nothing."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


@dataclass(slots=True)
class _ExtractFilter:
    """The ``filter=`` callable: hard links refused, then ``data_filter``.

    Beyond the hard-link refusal and the symlink skip it holds the inner tar to
    the member index the Archive was verified against (verification never opens
    the payload, ADR-0024): a name twice, a member below a symlink this Archive
    wrote, a v1 member whose header kind is not its indexed kind, and a member
    that resolves onto an excluded path are each refused, so nothing reaches an
    excluded file under another name.
    """

    view: ArchiveView
    roles: frozenset[MemberRole]
    exclude: frozenset[PurePosixPath]
    destination: Path
    kinds: Mapping[str, MemberKind]
    check_kinds: bool
    #: Indexed sizes (``ViewMember.size``), ``None`` for a v0 member, whose
    #: header's own size is what tarfile writes (BL-E002-116).
    sizes: Mapping[str, int | None] = field(default_factory=dict)
    #: The sum of the indexed sizes of the roles being extracted, or ``None``
    #: when any of them is unindexed (v0).
    cap: int | None = None
    members: int = 0
    bytes: int = 0
    skipped: list[SkippedLink] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    links: set[PurePosixPath] = field(default_factory=set)
    #: Directories accepted, by name: the very ``TarInfo`` passed, and its
    #: role-relative name. Patched interpreters (3.11.13+, 3.12.11+) call the
    #: filter a second time with that same object to fix directory attributes.
    directories: dict[str, tuple[tarfile.TarInfo, str]] = field(default_factory=dict)
    #: ``(st_dev, st_ino)`` of each excluded entry present before extraction;
    #: ``None`` until the first member. On a case-insensitive filesystem
    #: ``WP-CONFIG.PHP`` is ``wp-config.php`` under another spelling, which
    #: only the inode shows (BL-E002-067).
    excluded_identities: frozenset[tuple[int, int]] | None = None

    def _excluded(self, root: Path) -> frozenset[tuple[int, int]]:
        if self.excluded_identities is None:
            found = (_existing_identity(root / entry) for entry in self.exclude)
            self.excluded_identities = frozenset(i for i in found if i is not None)
        return self.excluded_identities

    def _skip_link(self, relative: str, target: str, reason: _SkipReason) -> None:
        self.skipped.append(
            SkippedLink(path=relative, link_target=target, reason=reason)
        )
        _log.warning(
            "symlink_skipped",
            path=repr(relative),
            link_target=repr(target),
            reason=reason,
        )

    def __call__(self, member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        name = member.name
        if member.islnk():
            # data_filter allows a hard link that stays inside the destination;
            # ADR-0017 never writes one, so it is only ever crafted.
            raise ExtractionRefusedError(name, "a hard link")
        fixup = self.directories.get(name)
        if fixup is not None and fixup[0] is member:
            # The stdlib's directory fix-up pass, not a second member: re-filter
            # against the filesystem as it is now and count nothing. Its own
            # FilterError handling skips the fix-up if this refuses.
            return tarfile.data_filter(member.replace(name=fixup[1], deep=False), path)
        if name in self.seen:
            raise ExtractionRefusedError(name, "occurs twice in the payload")
        self.seen.add(name)
        if _is_reserved(name):
            # The inner Manifest is read by read_inner_manifest, never written
            # into a Site; only an encrypted payload carries it, as a file.
            if name != INNER_MANIFEST_MEMBER or not self.view.encrypted:
                raise ExtractionRefusedError(
                    name, "under the reserved inner Manifest name"
                )
            if not member.isfile():
                raise ExtractionRefusedError(
                    name, "under the reserved inner Manifest name"
                )
            return None
        indexed = self.kinds.get(name)
        if indexed is None:
            raise ExtractionRefusedError(
                name, "not among the members the archive lists"
            )
        located = member_relative(self.view, name)
        if located is None:
            raise ExtractionRefusedError(name, "escapes its role prefix")
        role, relative = located
        declared = self.sizes.get(name)
        if member.isfile() and declared is not None and member.size > declared:
            # Before the role check (BL-E003-088): a member of a role that is
            # not extracted is still decompressed to be skipped, so its header
            # is held to its indexed size too, and the whole payload's work is
            # bounded by the index.
            reason = (
                f"its size, {member.size} bytes, exceeds the {declared} bytes "
                "the archive lists for it"
            )
            raise ExtractionRefusedError(name, reason)
        if role not in self.roles or not relative:
            return None
        if any(parent in self.links for parent in PurePosixPath(relative).parents):
            raise ExtractionRefusedError(name, "below a symlink in this archive")
        renamed = member.replace(name=relative, deep=False)
        try:
            filtered = tarfile.data_filter(renamed, path)
        except tarfile.AbsoluteLinkError as exc:
            if not member.issym():  # pragma: no cover - hard links refused above
                raise ExtractionRefusedError(name, type(exc).__name__) from exc
            self._skip_link(relative, member.linkname, "absolute")
            return None
        except tarfile.LinkOutsideDestinationError as exc:
            if not member.issym():  # pragma: no cover - hard links refused above
                raise ExtractionRefusedError(name, type(exc).__name__) from exc
            self._skip_link(relative, member.linkname, "outside_destination")
            return None
        except tarfile.FilterError as exc:
            raise ExtractionRefusedError(name, type(exc).__name__) from exc
        if self.check_kinds and _header_kind(member) is not indexed:
            reason = f"its type is not the {indexed.value} the archive lists for it"
            raise ExtractionRefusedError(name, reason)
        if PurePosixPath(relative) in self.exclude:
            return None
        root = self.destination.resolve()
        target = (root / filtered.name).resolve()
        if not target.is_relative_to(root):
            raise ExtractionRefusedError(name, "outside the destination")
        if member.isdir():
            self.directories[name] = (member, relative)
        if member.issym():
            self.links.add(PurePosixPath(relative))
        elif target in {(root / entry).resolve() for entry in self.exclude} or (
            _existing_identity(target) in self._excluded(root)
        ):
            raise ExtractionRefusedError(name, "resolves onto an excluded path")
        written = filtered.size if filtered.isfile() else 0
        if self.cap is not None and self.bytes + written > self.cap:
            reason = f"the running total passes the {self.cap} bytes the archive lists"
            raise ExtractionRefusedError(name, reason)
        self.members += 1
        self.bytes += written
        return filtered


#: The refusal on an interpreter without the June 2025 tarfile security fixes.
_UNPATCHED_TARFILE: Final = (
    "this Python lacks the June 2025 tarfile security fixes (CVE-2025-4517, "
    "CVE-2025-4330, CVE-2025-4138, CVE-2024-12718: os.path.ALLOW_MISSING is "
    "absent), so its data filter can be escaped; upgrade Python to a patched "
    "release (3.11.13+, 3.12.11+, 3.13.4+)"
)


def require_patched_tarfile() -> None:
    """Refuse an interpreter whose ``tarfile`` data filter can be escaped.

    The one capability check (Shawn, 2026-11-03): :func:`extract_archive`
    calls it first, and the restore's ``PREFLIGHT`` calls it before ``CLEAN``,
    so a refusal comes before anything is deleted. No ``tarfile.data_filter``,
    or no ``os.path.ALLOW_MISSING`` (added by the June 2025 fixes, CVE-2025-4517,
    -4330, -4138, CVE-2024-12718 — tested by capability, not version number),
    raises :class:`ExtractionRefusedError`.
    """
    if not hasattr(tarfile, "data_filter"):
        raise ExtractionRefusedError(None, "this Python's tarfile has no data filter")
    if not hasattr(os.path, "ALLOW_MISSING"):
        raise ExtractionRefusedError(None, _UNPATCHED_TARFILE)


def _check_exclude(exclude: frozenset[PurePosixPath]) -> None:
    for entry in exclude:
        if entry.is_absolute() or ".." in entry.parts or not entry.parts:
            msg = f"exclude entry {str(entry)!r} must be relative, with no '..'"
            raise ValueError(msg)


def extract_archive(
    source: StorageTarget,
    destination: Path,
    view: ArchiveView,
    *,
    roles: frozenset[MemberRole],
    exclude: frozenset[PurePosixPath] = frozenset(),
    payload: Path | None = None,
) -> ExtractResult:
    """Extract ``roles``' members of ``view`` into ``destination`` (AD-11).

    The members are read from the plain payload — ``payload`` when given (the
    file :func:`decrypt_payload` wrote), else the outer tar's payload member,
    ``ValueError`` for an encrypted view without one — or, for v0, from the
    Archive itself. Each member is renamed below its role prefix and passed to
    ``tarfile.data_filter``; a symlink it refuses for its target is skipped and
    reported in ``skipped_links``, any other refusal, a hard link anywhere and
    a member outside the index raise :class:`ExtractionRefusedError`. ``exclude``
    holds role-relative paths whose bytes are never written. A file member
    whose header size exceeds its indexed ``ViewMember.size`` is refused —
    in a role that is not extracted too, since it is still decompressed to be
    skipped (BL-E003-088) — and
    so is a running total past the sum of the extracted roles' indexed sizes
    (BL-E002-116); a v0 member has no indexed size, and tarfile writes no more
    than its header states. :func:`require_patched_tarfile` runs first, before
    anything is read. Only this function is gated: ``read_member`` and
    ``decrypt_payload`` stream members and write none to a destination.
    Members written before a refusal stay written. A ``payload`` that cannot be
    opened is :class:`ArchiveReadError`; any other ``OSError`` while extracting
    is :class:`ExtractionWriteError` naming ``destination`` as what it is (the
    Site Root, for the ``site-root`` role).
    """
    _check_exclude(exclude)
    require_patched_tarfile()
    archive = _archive_path(source)
    sizes = {m.path: m.size for m in view.members}
    extracted = [
        m
        for m in view.members
        if (located := member_relative(view, m.path)) is not None
        and located[0] in roles
        and m.kind is MemberKind.FILE
    ]
    cap = (
        None
        if any(m.size is None for m in extracted)
        else sum(m.size or 0 for m in extracted)
    )
    extract = _ExtractFilter(
        view=view,
        roles=roles,
        exclude=exclude,
        destination=destination,
        kinds={m.path: m.kind for m in view.members},
        # v0 kinds are synthesized from names (no index), so only v1's are held.
        check_kinds=view.payload is not None,
        sizes=sizes,
        cap=cap,
    )
    try:
        with _inner(archive, view, payload) as tar:
            # The one extraction (AD-11): filtered by the data_filter wrapper.
            tar.extractall(destination, filter=extract)  # noqa: S202
    except tarfile.FilterError as exc:  # pragma: no cover - the callable converts
        raise ExtractionRefusedError(None, type(exc).__name__) from exc
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise ArchiveReadError(payload or archive, _reason(exc)) from exc
    except OSError as exc:
        kind = (
            "the Site Root"
            if roles == {MemberRole.SITE_ROOT}
            else "the directory wp-config.php is laid down in"
            if roles == {MemberRole.CONFIG}
            else "the extraction directory"
        )
        raise ExtractionWriteError(destination, _reason(exc), destination=kind) from exc
    return ExtractResult(
        members=extract.members,
        bytes=extract.bytes,
        skipped_links=tuple(extract.skipped),
    )


def decrypt_payload(
    source: StorageTarget,
    view: ArchiveView,
    *,
    cipher: PayloadCipher,
    destination: Path,
) -> Path:
    """Decrypt the payload into ``destination``, a new ``0600`` file (ADR-0024).

    Streams the outer tar's payload member through ``cipher.decrypt``. Any
    failure — the cipher's own errors propagate unchanged — removes
    ``destination``. An ``OSError`` reading the Archive is
    :class:`ArchiveReadError`, one writing ``destination``
    :class:`ArchiveWriteError` (BL-E002-049). A view that
    is not encrypted raises ``ValueError``. A wrong passphrase or a damaged
    stream is logged first as ERROR
    ``member_decryption_failed`` with the payload's name and ``reason``
    (``wrong_passphrase`` or ``corrupt``).
    """
    if not view.encrypted or view.payload is None:
        msg = "decrypt_payload needs an encrypted v1 view"
        raise ValueError(msg)
    archive = _archive_path(source)
    try:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _STAGED_MODE,
        )
    except OSError as exc:
        raise ArchiveWriteError(destination, _reason(exc)) from exc
    reads: _ReadSide | None = None
    writes: _WriteSide | None = None
    try:
        with os.fdopen(fd, "wb") as writer, _outer_tar(archive) as outer:
            os.fchmod(writer.fileno(), _STAGED_MODE)
            reads = _ReadSide(_payload_body(outer, view.payload.name, archive))
            writes = _WriteSide(writer)
            cipher.decrypt(io.BufferedReader(reads), cast("IO[bytes]", writes))
            writes.flush()
    except (PassphraseRejectedError, PayloadDamagedError) as exc:
        destination.unlink(missing_ok=True)
        reason = (
            "wrong_passphrase"
            if isinstance(exc, PassphraseRejectedError)
            else "corrupt"
        )
        _log.error("member_decryption_failed", member=view.payload.name, reason=reason)
        raise
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        destination.unlink(missing_ok=True)
        raise ArchiveReadError(archive, _reason(exc)) from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)
        if reads is not None and reads.failed is exc:
            raise ArchiveReadError(archive, _reason(exc)) from exc
        if writes is not None and writes.failed is not None:
            # This one, or the close flushing what that write left buffered.
            raise ArchiveWriteError(destination, _reason(exc)) from exc
        raise  # the cipher's own error, unchanged
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _inner_manifest_failure(record: PhaseRecord, reason: str) -> PayloadDamagedError:
    record.fields["reason"] = reason
    return PayloadDamagedError()


def read_inner_manifest(
    payload: Path, *, phase_key: PhaseKey = BackupStep.VERIFY
) -> bytes:
    """The inner Manifest's bytes from the decrypted ``payload`` (ADR-0024 amendment).

    Streams the gzip tar :func:`decrypt_payload` wrote once, reading at most
    :data:`MANIFEST_MAX_BYTES` of the reserved member (a larger or non-file
    one reads as empty, so the comparison finds it unparsable). It must occur
    exactly once and last; otherwise the open ``phase_key`` record gets
    ``reason`` ``inner_manifest_missing``, ``inner_manifest_duplicated`` or
    ``inner_manifest_misplaced`` and :class:`PayloadDamagedError` is raised.
    A payload that is not a readable gzip tar is :class:`ArchiveReadError`.
    """
    with join_phase(phase_key) as record:
        found = 0
        last = False
        text = b""
        try:
            with tarfile.open(payload, mode="r|gz", tarinfo=_BoundedTarInfo) as tar:
                for info in tar:
                    last = info.name == INNER_MANIFEST_MEMBER
                    if not last:
                        continue
                    found += 1
                    handle = tar.extractfile(info) if info.isfile() else None
                    if handle is not None and info.size <= MANIFEST_MAX_BYTES:
                        text = handle.read(info.size)
        except _STREAM_ERRORS as exc:
            raise ArchiveReadError(payload, _reason(exc)) from exc
        if found == 0:
            raise _inner_manifest_failure(record, "inner_manifest_missing")
        if found > 1:
            raise _inner_manifest_failure(record, "inner_manifest_duplicated")
        if not last:
            raise _inner_manifest_failure(record, "inner_manifest_misplaced")
    return text
