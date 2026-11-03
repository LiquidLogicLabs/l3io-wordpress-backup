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

The Manifest carries **no key material, no KDF parameters and no salt**
(ADR-0014, ADR-0024), and its ``source`` is the Site Descriptor projected
without any credential. JSON is stdlib :mod:`json`; reads ignore unknown keys,
and optional keys are written only when set, so a Manifest without them is
byte-identical to one written before they existed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, TypeVar, cast

from l3io.wp.backup.domain.errors import MalformedManifestError, UnsafeMemberPathError
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.domain.site import SiteDescriptor

__all__ = [
    "CONFIG_RELATIVE",
    "DUMP_RELATIVE",
    "ENCRYPTED_PAYLOAD_MEMBER",
    "FORMAT_VERSION",
    "MANIFEST_MEMBER",
    "PAYLOAD_MEMBER",
    "RESERVED_SOURCE_KEYS",
    "ConfigLocation",
    "Manifest",
    "ManifestExclusions",
    "ManifestMember",
    "ManifestOrigin",
    "ManifestPayload",
    "MemberKind",
    "MemberRole",
    "OriginKind",
    "SourceValue",
    "manifest_from_json",
    "manifest_to_json",
    "member_path",
    "source_from_descriptor",
]

FORMAT_VERSION: Final = 1
MANIFEST_MEMBER: Final = "manifest.json"
PAYLOAD_MEMBER: Final = "payload.tar.gz"
ENCRYPTED_PAYLOAD_MEMBER: Final = "payload.tar.gz.age"
#: The Dump's name under the ``dump`` role, and the located config's under the
#: ``config`` role: ``member_path(MemberRole.DUMP, DUMP_RELATIVE)`` and
#: ``member_path(MemberRole.CONFIG, CONFIG_RELATIVE)``.
DUMP_RELATIVE: Final = "database.sql"
CONFIG_RELATIVE: Final = "wp-config.php"

#: ``source`` keys reserved for the stored ``siteurl``/``home`` options. Written
#: only when ``l3io-wordpress-database`` exposes a read of them as the
#: WordPress account; at v0.2.x it does not, so they are absent.
RESERVED_SOURCE_KEYS: Final = ("stored_siteurl", "stored_home")

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
    return json.dumps(document, indent=2) + "\n"


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
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"not JSON: {exc.msg} at line {exc.lineno}"
        raise MalformedManifestError(msg) from exc
    doc = _object(raw, "manifest")
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


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{where} must be an object"
        raise MalformedManifestError(msg)
    return cast("dict[str, object]", value)


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{where} must be an array"
        raise MalformedManifestError(msg)
    return cast("list[object]", value)


def _str(doc: Mapping[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise MalformedManifestError(msg)
    return value


def _optional_str(doc: Mapping[str, object], key: str) -> str | None:
    return None if doc.get(key) is None else _str(doc, key)


def _int(doc: Mapping[str, object], key: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer"
        raise MalformedManifestError(msg)
    return value


def _size(doc: Mapping[str, object], key: str) -> int:
    value = _int(doc, key)
    if value < 0:
        msg = f"{key} must not be negative"
        raise MalformedManifestError(msg)
    return value


def _bool(doc: Mapping[str, object], key: str) -> bool:
    value = doc.get(key)
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean"
        raise MalformedManifestError(msg)
    return value


def _source_value(key: str, value: object) -> SourceValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    msg = f"source.{key} must be a string, an integer or null"
    raise MalformedManifestError(msg)


def _payload(doc: Mapping[str, object]) -> ManifestPayload:
    name = _str(doc, "name")
    if name not in _PAYLOAD_NAMES:
        msg = f"payload.name {name!r} is not a payload member name"
        raise MalformedManifestError(msg)
    digest = _str(doc, "sha256")
    if len(digest) != _SHA256_HEX_LENGTH or not set(digest) <= _HEX_DIGITS:
        msg = "payload.sha256 must be 64 lowercase hex digits"
        raise MalformedManifestError(msg)
    return ManifestPayload(name=name, size=_size(doc, "size"), sha256=digest)


def _enum(kind: type[_E], doc: Mapping[str, object], key: str) -> _E:
    value = _str(doc, key)
    try:
        return kind(value)
    except ValueError as exc:
        msg = f"{key} {value!r} is not a known {kind.__name__}"
        raise MalformedManifestError(msg) from exc


def _member(doc: Mapping[str, object]) -> ManifestMember:
    kind = _enum(MemberKind, doc, "kind")
    role = _enum(MemberRole, doc, "role")
    size = _size(doc, "size")
    target = _optional_str(doc, "link_target")
    path = _str(doc, "path")
    if (kind is MemberKind.SYMLINK) != (target is not None):
        msg = f"member {path!r}: link_target is set exactly when kind is symlink"
        raise MalformedManifestError(msg)
    if kind is not MemberKind.FILE and size != 0:
        msg = f"member {path!r}: a {kind.value} has size 0"
        raise MalformedManifestError(msg)
    if kind is MemberKind.DIR and role is not MemberRole.SITE_ROOT:
        msg = f"member {path!r}: a dir belongs to the site-root role"
        raise MalformedManifestError(msg)
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
        raise MalformedManifestError(msg)
    patterns = _list(doc.get("patterns"), "exclusions.patterns")
    if not all(isinstance(p, str) for p in patterns):
        msg = "exclusions.patterns must be strings"
        raise MalformedManifestError(msg)
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
