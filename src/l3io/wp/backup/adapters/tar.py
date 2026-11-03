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
the workspace, then (with a cipher) the encrypted payload beside it, then the
uncompressed outer tar into ``out`` — the Manifest first, then the payload.
Each copy is a fixed-size chunked stream, so memory is bounded independent of
file and Archive sizes (AD-21); it is linear in the number of entries, which
the Manifest's member index lists. Every member is re-opened by path and
refused unless it is the same inode (device and inode number) the traversal
recorded, so nothing swapped in after the walk — through a renamed parent
directory included — is written. Workspace disk use peaks at the encryption step:
the Dump, the plain payload and the encrypted payload — the Dump plus about two
compressed payloads. Each payload file is deleted once copied.

**The publish gate.** :func:`structural_check` is the verifier the store runs
before publishing, until E002-S01-003 replaces it with ``verify_archive``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import posixpath
import stat
import tarfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Final, cast

import structlog

from l3io.wp.backup.adapters.phase import phase
from l3io.wp.backup.core.backup import BackupStep
from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ConfigLocation,
    Manifest,
    ManifestMember,
    ManifestPayload,
    MemberKind,
    MemberRole,
    manifest_from_json,
    manifest_to_json,
    member_path,
)
from l3io.wp.backup.domain.errors import (
    ArchiveWriteError,
    BackupError,
    MalformedManifestError,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.ports.crypto import PayloadCipher
from l3io.wp.backup.ports.exclusion import ExclusionPolicy

__all__ = [
    "ArchiveMemberSource",
    "SiteRootCollection",
    "collect_site_root",
    "file_member",
    "structural_check",
    "write_archive",
]

_log = structlog.get_logger(__name__)

#: The Site Root's own config, which no exclusion removes.
_CONFIG_NAME: Final = ConfigLocation.SITE_ROOT.value
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_STAGED_MODE: Final = 0o600
#: Inner members are written role by role, the Dump first (ADR-0024).
_ROLE_ORDER: Final = {MemberRole.DUMP: 0, MemberRole.CONFIG: 1, MemberRole.SITE_ROOT: 2}
_STRUCTURAL_FAILURE: Final = "structural check failed"


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
    except Exception as exc:
        reason = f"the exclusion policy failed deciding {relative!r}: {exc}"
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
    try:
        fd = os.open(member.source, os.O_RDONLY | os.O_NOFOLLOW)
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


def _write_payload(
    destination: Path, members: Iterable[ArchiveMemberSource]
) -> tuple[tuple[ManifestMember, ...], _HashingWriter]:
    ordered = sorted(members, key=lambda member: _ROLE_ORDER[member.role])
    with destination.open("xb") as raw:
        hashed = _HashingWriter(raw)
        with tarfile.open(fileobj=cast("IO[bytes]", hashed), mode="w|gz") as inner:
            index = tuple(_add(inner, member) for member in ordered)
    return index, hashed


def _encrypt(cipher: PayloadCipher, plain: Path, destination: Path) -> _HashingWriter:
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
    mtime = time.time()
    with tarfile.open(fileobj=out, mode="w|") as outer:
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
) -> Manifest:
    """Write one v1 Archive into ``out`` and return its Manifest (ADR-0024).

    ``manifest_builder`` receives the member index computed here and returns
    the Manifest; ``encrypted`` and ``payload`` are then set from what was
    written. The Manifest never passes through the cipher. Any failure removes
    the payload files from ``workspace``; an ``OSError`` or ``tarfile`` error
    is re-raised as :class:`ArchiveWriteError`.
    """
    plain = workspace / PAYLOAD_MEMBER
    sealed = workspace / ENCRYPTED_PAYLOAD_MEMBER
    writing = plain
    try:
        index, hashed = _write_payload(plain, members)
        payload, name = plain, PAYLOAD_MEMBER
        if cipher is not None:
            writing = sealed
            hashed = _encrypt(cipher, plain, sealed)
            payload, name = sealed, ENCRYPTED_PAYLOAD_MEMBER
        manifest = dataclasses.replace(
            manifest_builder(index),
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
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        raise ArchiveWriteError(writing, reason) from exc
    finally:
        plain.unlink(missing_ok=True)
        sealed.unlink(missing_ok=True)
    return manifest


# --------------------------------------------------------------------------
# The publish gate
# --------------------------------------------------------------------------


class _StructuralError(BackupError):
    """Internal: why the structural check failed. Never leaves this module."""


def _check(outer: tarfile.TarFile) -> None:
    first = outer.next()
    if first is None or first.name != MANIFEST_MEMBER or not first.isfile():
        msg = "the first member is not the Manifest"
        raise _StructuralError(msg)
    handle = outer.extractfile(first)
    if handle is None:  # pragma: no cover - isfile() guarantees a handle
        msg = "the Manifest has no content"
        raise _StructuralError(msg)
    manifest = manifest_from_json(handle.read().decode("utf-8"))
    if manifest.format_version != FORMAT_VERSION:
        msg = f"format_version {manifest.format_version} is not {FORMAT_VERSION}"
        raise _StructuralError(msg)
    expected = ENCRYPTED_PAYLOAD_MEMBER if manifest.encrypted else PAYLOAD_MEMBER
    second = outer.next()
    if (
        second is None
        or not second.isfile()
        or second.name != manifest.payload.name
        or second.name != expected
        or second.size != manifest.payload.size
    ):
        msg = "the second member is not the payload the Manifest names"
        raise _StructuralError(msg)
    body = outer.extractfile(second)
    if body is None:  # pragma: no cover - isfile() guarantees a handle
        msg = "the payload has no content"
        raise _StructuralError(msg)
    if (
        hashlib.file_digest(cast("io.BufferedReader", body), "sha256").hexdigest()
        != manifest.payload.sha256
    ):
        msg = "the payload's SHA-256 does not match the Manifest"
        raise _StructuralError(msg)
    if outer.next() is not None:
        msg = "a member follows the payload"
        raise _StructuralError(msg)


def structural_check(path: Path) -> None:
    """Raise unless ``path`` is structurally a v1 Archive (ADR-0024).

    Reads the file in stream mode: the Manifest must be the first member and
    parse, and the only other member must be the payload it names, of the size
    and SHA-256 it records. E002-S01-003 replaces this with ``verify_archive``.
    """
    try:
        with tarfile.open(path, mode="r|") as outer:
            _check(outer)
    except (
        _StructuralError,
        MalformedManifestError,
        OSError,
        UnicodeDecodeError,
        tarfile.TarError,
    ) as exc:
        _log.warning("structural_check_failed", path=str(path), detail=str(exc))
        raise ArchiveWriteError(path, _STRUCTURAL_FAILURE) from exc
