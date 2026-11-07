"""AD-6 — the archive format is one owned contract (ADR-0017, ADR-0024).

No other module defines a member name, the Manifest key set or the version
number; ``tests/contracts/test_archive_format_owner.py`` greps ``src/`` for the
member-name literals outside this module.

**The outer tar** is uncompressed and holds exactly two members, in order:

1. :data:`MANIFEST_MEMBER` — first, occurring exactly once, never encrypted,
   and not listed in its own member index;
2. the payload, named :data:`PAYLOAD_MEMBER`, or
   :data:`ENCRYPTED_PAYLOAD_MEMBER` when the Manifest says ``encrypted``.

**The payload** is a gzip-compressed inner tar holding the ``dump``-role member
first when the scope includes the database, then the other members in the
writer's order. Inner member names are :func:`member_path`'s role-prefixed
names. The Manifest's member index lists the inner tar: every inner member has
exactly one entry and every entry exactly one inner member. Mode, mtime, uid
and gid live in the tar headers only.

**The inner Manifest** (ADR-0024 amendment, 2026-11-06, BL-E002-115). An
encrypted payload ends with one more member, :data:`INNER_MANIFEST_MEMBER`:
the Manifest as :func:`inner_manifest_bytes` writes it, without the encrypted
payload's own size and sha256. It is last, not listed in the index it
carries, and never extracted. After decrypting, ``verify --authenticate`` and
the restore's ``VERIFY`` compare it with the outer Manifest through
:func:`check_inner_manifest`, which binds the unencrypted outer Manifest to
the payload ``age`` authenticated. A whole older Archive under the same
passphrase still passes: that rollback is a permanent limit.

The Manifest carries **no key material, no KDF parameters and no salt**
(ADR-0014, ADR-0024), and its ``source`` is the Site Descriptor projected
without any credential. JSON is stdlib :mod:`json`; reads ignore unknown keys,
and optional keys are written only when set, so a Manifest without them is
byte-identical to one written before they existed.

**Reading an Archive (AD-23).** :func:`classify` turns the raw facts an
``ArchiveProbe`` reports (:class:`MemberIndex`) into an :class:`ArchiveView`.
It recognises exactly three shapes — v1 (the Manifest first), v0 with a Dump
and v0 files-only — and this is the only module that branches on
``format_version`` (``tests/contracts/test_version_branching.py``). A v0
Archive is detected by its member names, never by a file name or extension,
and its roles are synthesized: ``database.sql`` is the ``dump``, ``wp-root/…``
the ``site-root``. v0 member *kinds* are synthesized too (``wp-root`` itself a
``dir``, every other member a ``file``), since a v0 Archive has no index; the
tar header stays authoritative at extraction.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, TypeVar, cast

from l3io.wp.backup.domain.errors import (
    MalformedManifestError,
    UnrecognisedArchiveError,
    UnsafeMemberPathError,
    UnsupportedFormatVersionError,
)
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.domain.site import SiteDescriptor

__all__ = [
    "CONFIG_RELATIVE",
    "DUMP_RELATIVE",
    "ENCRYPTED_PAYLOAD_MEMBER",
    "FORMAT_VERSION",
    "INNER_MANIFEST_MEMBER",
    "INNER_UNPARSABLE",
    "MANIFEST_MAX_BYTES",
    "MANIFEST_MEMBER",
    "PAYLOAD_MEMBER",
    "RESERVED_SOURCE_KEYS",
    "SUPPORTED_FORMAT_VERSIONS",
    "V0_DUMP_MEMBER",
    "V0_SITE_ROOT",
    "ArchiveView",
    "ConfigLocation",
    "ExtractResult",
    "Integrity",
    "Manifest",
    "ManifestExclusions",
    "ManifestMember",
    "ManifestOrigin",
    "ManifestPayload",
    "MemberDigest",
    "MemberIndex",
    "MemberKind",
    "MemberRole",
    "OriginKind",
    "SkippedLink",
    "SourceValue",
    "TarMemberType",
    "VerificationFailure",
    "VerificationReason",
    "VerificationResult",
    "ViewMember",
    "check_inner_manifest",
    "classify",
    "format_label",
    "inner_manifest_bytes",
    "is_safe_member_path",
    "manifest_from_json",
    "manifest_to_json",
    "member_path",
    "member_relative",
    "source_from_descriptor",
]

FORMAT_VERSION: Final = 1
#: Every ``format_version`` this build reads (ADR-0017). The committed fixture
#: Archives are the evidence it is right (``test_supported_formats.py``).
#: v0 has no ``format_version`` and is detected by shape, not through this.
SUPPORTED_FORMAT_VERSIONS: Final[frozenset[int]] = frozenset({1})
#: The one member read whole; anything larger is ``UNREADABLE`` (AD-11).
MANIFEST_MAX_BYTES: Final = 16 * 1024 * 1024
MANIFEST_MEMBER: Final = "manifest.json"
PAYLOAD_MEMBER: Final = "payload.tar.gz"
ENCRYPTED_PAYLOAD_MEMBER: Final = "payload.tar.gz.age"
#: The encrypted payload's last member: the Manifest less the payload's size
#: and sha256 (ADR-0024 amendment). Every other inner member carries a role
#: prefix (``dump/``, ``config/``, ``site-root/``), so it collides with none.
INNER_MANIFEST_MEMBER: Final = "inner-manifest.json"
#: The Dump's name under the ``dump`` role, and the located config's under the
#: ``config`` role: ``member_path(MemberRole.DUMP, DUMP_RELATIVE)`` and
#: ``member_path(MemberRole.CONFIG, CONFIG_RELATIVE)``.
DUMP_RELATIVE: Final = "database.sql"
CONFIG_RELATIVE: Final = "wp-config.php"

#: ``source`` keys reserved for the stored ``siteurl``/``home`` options. Written
#: only when ``l3io-wordpress-database`` exposes a read of them as the
#: WordPress account; at v0.2.x it does not, so they are absent.
RESERVED_SOURCE_KEYS: Final = ("stored_siteurl", "stored_home")

#: The two member names a v0 (0.2.21) Archive is recognised by.
V0_DUMP_MEMBER: Final = "database.sql"
V0_SITE_ROOT: Final = "wp-root"
_V0_CONFIG: Final = f"{V0_SITE_ROOT}/{CONFIG_RELATIVE}"
#: How many member names an ``UnrecognisedArchiveError`` quotes.
_FOUND_SHOWN: Final = 5

SourceValue = str | int | None

_PAYLOAD_NAMES: Final = frozenset({PAYLOAD_MEMBER, ENCRYPTED_PAYLOAD_MEMBER})
_SHA256_HEX_LENGTH: Final = 64
_HEX_DIGITS: Final = frozenset("0123456789abcdef")

_E = TypeVar("_E", bound=StrEnum)


class MemberRole(StrEnum):
    """The role prefix of an inner-tar member."""

    DUMP = "dump"
    SITE_ROOT = "site-root"
    CONFIG = "config"


class MemberKind(StrEnum):
    """What an inner-tar member is. A new kind is format v2 (ADR-0017)."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"


class ConfigLocation(StrEnum):
    """The two places ``wp-load.php`` searches for ``wp-config.php``."""

    SITE_ROOT = "wp-config.php"
    PARENT = "../wp-config.php"


class OriginKind(StrEnum):
    """Why an Archive was written, when not by an operator's backup."""

    SAFETY_SNAPSHOT = "safety_snapshot"


def member_path(role: MemberRole, relative: str) -> str:
    """The role-prefixed inner-tar name for ``relative``.

    ``relative == ""`` names the role's root itself (the Site Root ``dir``
    entry). Absolute paths, ``..``, ``.`` or empty segments (so no trailing
    slash), NULs and backslashes raise :class:`UnsafeMemberPathError`, so a
    member name can never leave its role prefix.
    """
    if "\x00" in relative:
        raise UnsafeMemberPathError(relative, "contains a NUL")
    if "\\" in relative:
        raise UnsafeMemberPathError(relative, "contains a backslash")
    if relative.startswith("/"):
        raise UnsafeMemberPathError(relative, "is absolute")
    if relative == "":
        return role.value
    for segment in relative.split("/"):
        if segment == "..":
            raise UnsafeMemberPathError(relative, "has a '..' segment")
        if segment in {"", "."}:
            raise UnsafeMemberPathError(relative, "is not in canonical form")
    return f"{role.value}/{relative}"


@dataclass(frozen=True, slots=True)
class ManifestMember:
    """One inner-tar member. Carries no checksum and no ``encrypted`` flag."""

    path: str
    role: MemberRole
    kind: MemberKind
    size: int
    link_target: str | None


@dataclass(frozen=True, slots=True)
class ManifestPayload:
    """The payload member: its name, and size and SHA-256 over the stored bytes."""

    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ManifestOrigin:
    """Why this Archive was written. ``restore_mode`` is a ``RestoreMode`` value."""

    kind: OriginKind
    restore_mode: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ManifestExclusions:
    """Which exclusion policy the backup applied, and what it left out."""

    source: Literal["default", "custom", "none"]
    patterns: tuple[str, ...]
    excluded: tuple[ExcludedEntry, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    """The v1 Manifest. Deeply immutable: ``source`` is a ``MappingProxyType``."""

    format_version: int
    created_at: str
    backup_scope: str
    source: Mapping[str, SourceValue]
    encrypted: bool
    payload: ManifestPayload
    members: tuple[ManifestMember, ...]
    dump_present: bool
    origin: ManifestOrigin | None = None
    exclusions: ManifestExclusions | None = None
    config_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "members", tuple(self.members))


def source_from_descriptor(descriptor: SiteDescriptor) -> Mapping[str, SourceValue]:
    """The descriptor projected for the Manifest, without any credential.

    ``credential_source`` is left out too: it names how credentials were found,
    which says nothing about the Site an Archive restores.
    """
    return MappingProxyType(
        {
            "site_root": str(descriptor.site_root),
            "db_host": descriptor.db_host,
            "db_port": descriptor.db_port,
            "db_name": descriptor.db_name,
            "table_prefix": descriptor.table_prefix,
            "site_url": descriptor.site_url,
            "home_url": descriptor.home_url,
        }
    )


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def manifest_to_json(manifest: Manifest) -> str:
    """Serialise with stdlib ``json``. Optional keys appear only when set.

    The v1 text is ASCII: stdlib's default ``ensure_ascii`` escapes every
    non-ASCII character, including the lone surrogates a non-UTF-8 file name
    decodes to (``os.fsdecode``), so the Manifest always encodes as UTF-8 and
    ``json.loads`` returns each string exactly.
    """
    return json.dumps(_document(manifest), indent=2) + "\n"


def _document(manifest: Manifest) -> dict[str, object]:
    document: dict[str, object] = {
        "format_version": manifest.format_version,
        "created_at": manifest.created_at,
        "backup_scope": manifest.backup_scope,
        "source": dict(manifest.source),
        "encrypted": manifest.encrypted,
        "payload": {
            "name": manifest.payload.name,
            "size": manifest.payload.size,
            "sha256": manifest.payload.sha256,
        },
        "members": [
            {
                "path": m.path,
                "role": m.role.value,
                "kind": m.kind.value,
                "size": m.size,
                "link_target": m.link_target,
            }
            for m in manifest.members
        ],
        "dump_present": manifest.dump_present,
    }
    if manifest.origin is not None:
        document["origin"] = {
            "kind": manifest.origin.kind.value,
            "restore_mode": manifest.origin.restore_mode,
            "run_id": manifest.origin.run_id,
        }
    if manifest.exclusions is not None:
        document["exclusions"] = {
            "source": manifest.exclusions.source,
            "patterns": list(manifest.exclusions.patterns),
            "excluded": [
                {"path": e.path, "pattern": e.pattern, "is_dir": e.is_dir}
                for e in manifest.exclusions.excluded
            ],
        }
    if manifest.config_path is not None:
        document["config_path"] = manifest.config_path
    return document


#: Called with ``(field, value)`` when a read drops a value this build does
#: not know, so the caller can log it (``domain/`` has no logger, AD-1).
UnknownValueHook = Callable[[str, str], None]


def manifest_from_json(
    text: str, *, on_unknown: UnknownValueHook | None = None
) -> Manifest:
    """Parse a Manifest. Unknown keys are ignored; malformed fields raise.

    An ``origin.kind`` this build does not know reads as ``origin=None`` and is
    reported through ``on_unknown("origin.kind", value)`` (forward
    compatibility). An unknown member ``kind`` is malformed.
    """
    return _manifest(_object(_loads(text), "manifest"), on_unknown)


def _manifest(
    doc: Mapping[str, object], on_unknown: UnknownValueHook | None
) -> Manifest:
    source = _object(doc.get("source"), "source")
    return Manifest(
        format_version=_int(doc, "format_version"),
        created_at=_str(doc, "created_at"),
        backup_scope=_str(doc, "backup_scope"),
        source={key: _source_value(key, value) for key, value in source.items()},
        encrypted=_bool(doc, "encrypted"),
        payload=_payload(_object(doc.get("payload"), "payload")),
        members=tuple(
            _member(_object(item, "members[]"))
            for item in _list(doc.get("members"), "members")
        ),
        dump_present=_bool(doc, "dump_present"),
        origin=_origin(doc.get("origin"), on_unknown),
        exclusions=_exclusions(doc.get("exclusions")),
        config_path=_optional_str(doc, "config_path"),
    )


def _loads(text: str) -> object:
    """``json.loads``, every way untrusted text can make it fail made malformed.

    Beyond ``JSONDecodeError``: an integer past Python's digit limit raises
    ``ValueError`` and deeply nested arrays ``RecursionError``.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"not JSON: {exc.msg} at line {exc.lineno}"
        raise MalformedManifestError(msg, field=None) from exc
    except (ValueError, RecursionError) as exc:
        msg = f"not readable JSON: {type(exc).__name__}"
        raise MalformedManifestError(msg, field=None) from exc


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{where} must be an object"
        raise MalformedManifestError(msg, field=where)
    return cast("dict[str, object]", value)


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{where} must be an array"
        raise MalformedManifestError(msg, field=where)
    return cast("list[object]", value)


def _str(doc: Mapping[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise MalformedManifestError(msg, field=key)
    return value


def _optional_str(doc: Mapping[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else _str(doc, key)


def _int(doc: Mapping[str, object], key: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer"
        raise MalformedManifestError(msg, field=key)
    return value


def _size(doc: Mapping[str, object], key: str) -> int:
    value = _int(doc, key)
    if value < 0:
        msg = f"{key} must not be negative"
        raise MalformedManifestError(msg, field=key)
    return value


def _bool(doc: Mapping[str, object], key: str) -> bool:
    value = doc.get(key)
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise MalformedManifestError(msg, field=key)
    return value


def _source_value(key: str, value: object) -> SourceValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    msg = f"source.{key} must be a string, an integer or null"
    raise MalformedManifestError(msg, field=f"source.{key}")


def _payload(doc: Mapping[str, object]) -> ManifestPayload:
    name = _str(doc, "name")
    if name not in _PAYLOAD_NAMES:
        msg = f"payload.name {name!r} is not a payload member name"
        raise MalformedManifestError(msg, field="payload.name")
    digest = _str(doc, "sha256")
    if len(digest) != _SHA256_HEX_LENGTH or not set(digest) <= _HEX_DIGITS:
        msg = "payload.sha256 must be 64 lowercase hex digits"
        raise MalformedManifestError(msg, field="payload.sha256")
    return ManifestPayload(name=name, size=_size(doc, "size"), sha256=digest)


def _enum(kind: type[_E], doc: Mapping[str, object], key: str) -> _E:
    value = _str(doc, key)
    try:
        return kind(value)
    except ValueError as exc:
        msg = f"{key} {value!r} is not a known {kind.__name__}"
        raise MalformedManifestError(msg, field=key) from exc


def _member(doc: Mapping[str, object]) -> ManifestMember:
    kind = _enum(MemberKind, doc, "kind")
    role = _enum(MemberRole, doc, "role")
    size = _size(doc, "size")
    target = _optional_str(doc, "link_target")
    path = _str(doc, "path")
    if (kind is MemberKind.SYMLINK) != (target is not None):
        msg = f"member {path!r}: link_target is set exactly when kind is symlink"
        raise MalformedManifestError(msg, field="link_target")
    if kind is not MemberKind.FILE and size != 0:
        msg = f"member {path!r}: a {kind.value} has size 0"
        raise MalformedManifestError(msg, field="size")
    if kind is MemberKind.DIR and role is not MemberRole.SITE_ROOT:
        msg = f"member {path!r}: a dir belongs to the site-root role"
        raise MalformedManifestError(msg, field="role")
    return ManifestMember(
        path=path, role=role, kind=kind, size=size, link_target=target
    )


def _origin(
    value: object, on_unknown: UnknownValueHook | None
) -> ManifestOrigin | None:
    if value is None:
        return None
    doc = _object(value, "origin")
    kind = _str(doc, "kind")
    restore_mode = _str(doc, "restore_mode")
    run_id = _str(doc, "run_id")
    try:
        known = OriginKind(kind)
    except ValueError:
        if on_unknown is not None:
            on_unknown("origin.kind", kind)
        return None
    return ManifestOrigin(kind=known, restore_mode=restore_mode, run_id=run_id)


_EXCLUSION_SOURCES: Final = ("default", "custom", "none")


def _exclusions(value: object) -> ManifestExclusions | None:
    if value is None:
        return None
    doc = _object(value, "exclusions")
    source = _str(doc, "source")
    if source not in _EXCLUSION_SOURCES:
        msg = f"exclusions.source {source!r} is not one of {_EXCLUSION_SOURCES}"
        raise MalformedManifestError(msg, field="exclusions.source")
    patterns = _list(doc.get("patterns"), "exclusions.patterns")
    if not all(isinstance(p, str) for p in patterns):
        msg = "exclusions.patterns must be strings"
        raise MalformedManifestError(msg, field="exclusions.patterns")
    excluded = []
    for item in _list(doc.get("excluded"), "exclusions.excluded"):
        entry = _object(item, "exclusions.excluded[]")
        excluded.append(
            ExcludedEntry(
                path=_str(entry, "path"),
                pattern=_str(entry, "pattern"),
                is_dir=_bool(entry, "is_dir"),
            )
        )
    return ManifestExclusions(
        source=cast("Literal['default', 'custom', 'none']", source),
        patterns=tuple(cast("list[str]", patterns)),
        excluded=tuple(excluded),
    )


# --------------------------------------------------------------------------
# The inner Manifest (ADR-0024 amendment, 2026-11-06)
# --------------------------------------------------------------------------

#: The payload fields the inner Manifest leaves out: they describe the
#: encrypted payload, which cannot contain its own size or hash.
_INNER_LEFT_OUT: Final = ("size", "sha256")
#: What both sides' left-out fields are set to before the comparison.
_LEFT_OUT_SIZE: Final = 0
_LEFT_OUT_SHA256: Final = "0" * _SHA256_HEX_LENGTH
#: :func:`check_inner_manifest`'s answer for an inner document it cannot read.
INNER_UNPARSABLE: Final = "<unparsable>"


def inner_manifest_bytes(manifest: Manifest) -> bytes:
    """The inner Manifest's bytes: ``manifest`` without the payload's size and sha256.

    Its own serialiser, because :func:`manifest_to_json` requires both fields.
    Everything else is written as :func:`manifest_to_json` writes it.
    """
    document = _document(manifest)
    payload = cast("dict[str, object]", document["payload"])
    for key in _INNER_LEFT_OUT:
        del payload[key]
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def _without_payload_digest(manifest: Manifest) -> Manifest:
    return replace(
        manifest,
        payload=ManifestPayload(
            name=manifest.payload.name, size=_LEFT_OUT_SIZE, sha256=_LEFT_OUT_SHA256
        ),
    )


def _inner_manifest(inner_bytes: bytes) -> Manifest:
    """Parse the inner document with the outer parser's field rules."""
    doc = _object(_loads(inner_bytes.decode("utf-8")), "manifest")
    payload = _object(doc.get("payload"), "payload")
    doc = {
        **doc,
        "payload": {
            **payload,
            "size": _LEFT_OUT_SIZE,
            "sha256": _LEFT_OUT_SHA256,
        },
    }
    return _manifest(doc, None)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _first_difference(outer: object, inner: object, path: str) -> str | None:
    """The first path at which ``outer`` and ``inner`` differ, field by field."""
    if is_dataclass(outer) and type(outer) is type(inner):
        for item in fields(outer):
            found = _first_difference(
                getattr(outer, item.name),
                getattr(inner, item.name),
                _join(path, item.name),
            )
            if found is not None:
                return found
        return None
    if isinstance(outer, Mapping) and isinstance(inner, Mapping):
        outer_map = cast("Mapping[str, object]", outer)
        inner_map = cast("Mapping[str, object]", inner)
        for key in sorted(outer_map.keys() | inner_map.keys()):
            if key not in outer_map or key not in inner_map:
                # A key on one side only may be the unauthenticated outer
                # Manifest's own text: name the mapping, never the key.
                return path
            found = _first_difference(outer_map[key], inner_map[key], _join(path, key))
            if found is not None:
                return found
        return None
    if isinstance(outer, tuple) and isinstance(inner, tuple):
        if len(outer) != len(inner):
            return path
        for position, (left, right) in enumerate(zip(outer, inner, strict=True)):
            found = _first_difference(left, right, f"{path}[{position}]")
            if found is not None:
                return found
        return None
    if type(outer) is not type(inner) or outer != inner:
        return path
    return None


def check_inner_manifest(outer_text: str, inner_bytes: bytes) -> str | None:
    """Compare the outer Manifest with the payload's inner one, field by field.

    ``outer_text`` is the verified outer Manifest, parsed with
    :func:`manifest_from_json`; the payload's size and sha256 are left out of
    the comparison. Returns ``None`` when the two are equal, else the first
    differing field's path (``source.site_url``, ``members[3].path``), never
    a value; an inner document that does not parse is
    :data:`INNER_UNPARSABLE`. Both check sites call this; neither compares
    fields itself.
    """
    outer = _without_payload_digest(manifest_from_json(outer_text))
    try:
        inner = _inner_manifest(inner_bytes)
    except (MalformedManifestError, UnicodeDecodeError):
        return INNER_UNPARSABLE
    return _first_difference(outer, inner, "")


# --------------------------------------------------------------------------
# Reading an Archive: probe facts, the classifier and the verification result
# --------------------------------------------------------------------------


class TarMemberType(StrEnum):
    """What a tar header says a member is."""

    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class MemberIndex:
    """The raw facts a probe read from an Archive's outermost tar.

    ``names`` keeps duplicates in tar order. ``manifest_text`` is the first
    member's text when it is a regular ``manifest.json`` no larger than
    :data:`MANIFEST_MAX_BYTES` that decodes as UTF-8, else ``None``.
    ``readable_to_end`` is ``False`` when a stream that began as a tar (or as
    gzip) ended early or broke; a stream that is not a tar from its first
    header has no names and is readable to its end. ``gzip`` says the stream
    was gzip-compressed, detected from its first bytes.
    """

    names: tuple[str, ...]
    first_member: str | None
    manifest_text: str | None
    readable_to_end: bool
    gzip: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "names", tuple(self.names))


@dataclass(frozen=True, slots=True)
class MemberDigest:
    """One outer member as streamed: type, size and, for a file, its SHA-256."""

    tar_type: TarMemberType
    size: int
    sha256: str | None
    link_name: str | None


class Integrity(StrEnum):
    """What a passing verification proved."""

    VERIFIED = "VERIFIED"
    STRUCTURAL_ONLY = "STRUCTURAL_ONLY"


@dataclass(frozen=True, slots=True)
class ViewMember:
    """One member as a consumer sees it. No checksum, no ``encrypted`` flag.

    ``size`` is the Manifest member's declared size for v1 and ``None`` for v0,
    which has no Manifest; extraction enforces it (BL-E002-116).
    """

    path: str
    role: MemberRole
    kind: MemberKind
    link_target: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class ArchiveView:
    """An Archive classified: its format, members and what verification can prove.

    ``config_path`` is the v1 Manifest's value copied as found (Epic 7 converts
    it with ``ConfigLocation`` and refuses anything else, AD-11), or for v0
    ``"wp-root/wp-config.php"`` when that member is present. ``backup_scope``
    is the v1 Manifest's value, ``None`` for v0.
    """

    format_version: int | None
    members: tuple[ViewMember, ...]
    dump_present: bool
    config_path: str | None
    integrity: Integrity
    encrypted: bool
    payload: ManifestPayload | None
    backup_scope: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(self.members))


class VerificationReason(StrEnum):
    """Why a verification failed."""

    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    MEMBER_MISSING = "MEMBER_MISSING"
    UNDECLARED_MEMBER = "UNDECLARED_MEMBER"
    KIND_MISMATCH = "KIND_MISMATCH"
    UNSUPPORTED_MEMBER_TYPE = "UNSUPPORTED_MEMBER_TYPE"
    DUMP_DECLARED_ABSENT = "DUMP_DECLARED_ABSENT"
    TRUNCATED = "TRUNCATED"
    UNREADABLE = "UNREADABLE"
    UNRECOGNISED_FORMAT = "UNRECOGNISED_FORMAT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNSAFE_MEMBER_PATH = "UNSAFE_MEMBER_PATH"


@dataclass(frozen=True, slots=True)
class VerificationFailure:
    """One reason, and the member it concerns when there is one."""

    member: str | None
    reason: VerificationReason


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Pass or fail, and what a pass proved. ``failures`` is empty on a pass."""

    passed: bool
    integrity: Integrity
    format_version: int | None
    failures: tuple[VerificationFailure, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(self.failures))


@dataclass(frozen=True, slots=True)
class SkippedLink:
    """A symlink extraction skipped (ADR-0017 amendment). Target verbatim."""

    path: str
    link_target: str
    reason: Literal["absolute", "outside_destination"]


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """What an extraction wrote: member and byte counts, and the links skipped."""

    members: int
    bytes: int
    skipped_links: tuple[SkippedLink, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "skipped_links", tuple(self.skipped_links))


def format_label(format_version: int | None) -> int | str:
    """``format_version`` for a log record or display: the number, or ``"v0"``."""
    return "v0" if format_version is None else format_version


def is_safe_member_path(path: str, role: MemberRole) -> bool:
    """Whether a v1 member ``path`` is :func:`member_path`'s name under ``role``."""
    prefix = role.value
    if path == prefix:
        return True
    if not path.startswith(f"{prefix}/"):
        return False
    return _role_relative_ok(role, path[len(prefix) + 1 :])


def _role_relative_ok(role: MemberRole, relative: str) -> bool:
    """Whether ``relative`` is a safe name under ``role``.

    The ``dump`` and ``config`` roles each hold exactly one member, at the name
    the builder writes. Anything else under them would be extracted into the
    Run workspace or the config's directory (EP-RT-002).
    """
    try:
        member_path(role, relative)
    except UnsafeMemberPathError:
        return False
    if role is MemberRole.DUMP:
        return relative == DUMP_RELATIVE
    if role is MemberRole.CONFIG:
        return relative == CONFIG_RELATIVE
    return True


def _v0_role(name: str) -> MemberRole | None:
    if name == V0_DUMP_MEMBER:
        return MemberRole.DUMP
    if name == V0_SITE_ROOT or name.startswith(f"{V0_SITE_ROOT}/"):
        return MemberRole.SITE_ROOT
    return None


def member_relative(view: ArchiveView, path: str) -> tuple[MemberRole, str] | None:
    """``path``'s role and its name below the role prefix, or ``None``.

    ``None`` when ``path`` names no role or escapes its prefix. The role root
    itself (``site-root``, v0 ``wp-root``) is relative ``""``; the v0 Dump is
    relative ``database.sql``, as the v1 Dump is.
    """
    if view.payload is None:
        role = _v0_role(path)
        if role is MemberRole.DUMP:
            return role, DUMP_RELATIVE
        prefix = V0_SITE_ROOT
    else:
        role = next((r for r in MemberRole if path.split("/", 1)[0] == r.value), None)
        prefix = role.value if role is not None else ""
    if role is None:
        return None
    if path == prefix:
        return role, ""
    relative = path[len(prefix) + 1 :]
    if not _role_relative_ok(role, relative):
        return None
    return role, relative


def _found(names: tuple[str, ...]) -> tuple[str, ...]:
    return names[:_FOUND_SHOWN]


def _classify_v1(text: str | None) -> ArchiveView:
    if text is None:
        msg = "the Manifest is unreadable, not UTF-8 or larger than the cap"
        raise MalformedManifestError(msg, field=None)
    version = _int(_object(_loads(text), "manifest"), "format_version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise UnsupportedFormatVersionError(found=version)
    manifest = manifest_from_json(text)
    return ArchiveView(
        format_version=manifest.format_version,
        members=tuple(
            ViewMember(m.path, m.role, m.kind, m.link_target, m.size)
            for m in manifest.members
        ),
        dump_present=manifest.dump_present,
        config_path=manifest.config_path,
        integrity=Integrity.VERIFIED,
        encrypted=manifest.encrypted,
        payload=manifest.payload,
        backup_scope=manifest.backup_scope,
    )


def _classify_v0(names: tuple[str, ...]) -> ArchiveView:
    roles = [(name, _v0_role(name)) for name in names]
    has_dump = any(role is MemberRole.DUMP for _, role in roles)
    has_root = any(role is MemberRole.SITE_ROOT for _, role in roles)
    if not has_root or any(role is None for _, role in roles):
        raise UnrecognisedArchiveError(found=_found(names))
    members = tuple(
        ViewMember(
            path=name,
            role=cast("MemberRole", role),
            kind=MemberKind.DIR if name == V0_SITE_ROOT else MemberKind.FILE,
            link_target=None,
            size=None,
        )
        for name, role in roles
    )
    return ArchiveView(
        format_version=None,
        members=members,
        dump_present=has_dump,
        config_path=_V0_CONFIG if _V0_CONFIG in names else None,
        integrity=Integrity.STRUCTURAL_ONLY,
        encrypted=False,
        payload=None,
        backup_scope=None,
    )


def classify(index: MemberIndex) -> ArchiveView:
    """Recognise one of the three Archive shapes, or raise (AD-23).

    1. First member ``manifest.json`` → v1; its ``format_version`` must be in
       :data:`SUPPORTED_FORMAT_VERSIONS` (else
       :class:`UnsupportedFormatVersionError`); an unparseable Manifest raises
       :class:`MalformedManifestError`.
    2. No Manifest, ``database.sql`` and ``wp-root/…`` → v0 with a Dump.
    3. No Manifest, ``wp-root/…`` and no ``database.sql`` → v0 files-only.

    Anything else — a ``manifest.json`` that is not first, ``database.sql``
    without ``wp-root/``, a member outside both, no members at all — raises
    :class:`UnrecognisedArchiveError` naming the first few members found.
    """
    if index.first_member == MANIFEST_MEMBER:
        return _classify_v1(index.manifest_text)
    if MANIFEST_MEMBER in index.names:
        raise UnrecognisedArchiveError(found=_found(index.names))
    return _classify_v0(index.names)
