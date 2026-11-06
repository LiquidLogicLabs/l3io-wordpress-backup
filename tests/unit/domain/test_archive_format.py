"""AD-6 — the v1 archive format and Manifest (ADR-0017, ADR-0024)."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    FORMAT_VERSION,
    INNER_MANIFEST_MEMBER,
    INNER_UNPARSABLE,
    PAYLOAD_MEMBER,
    RESERVED_SOURCE_KEYS,
    Manifest,
    ManifestExclusions,
    ManifestMember,
    ManifestOrigin,
    ManifestPayload,
    MemberKind,
    MemberRole,
    OriginKind,
    SourceValue,
    check_inner_manifest,
    inner_manifest_bytes,
    is_safe_member_path,
    manifest_from_json,
    manifest_to_json,
    member_path,
    source_from_descriptor,
)
from l3io.wp.backup.domain.errors import MalformedManifestError, UnsafeMemberPathError
from l3io.wp.backup.domain.exclusion import ExcludedEntry
from l3io.wp.backup.domain.site import SiteDescriptor

SOURCE = {
    "site_root": "/srv/www",
    "db_host": "db",
    "db_port": 3306,
    "db_name": "wordpress",
    "table_prefix": "wp_",
    "site_url": None,
    "home_url": None,
}

#: Committed golden form of a Manifest with no optional key set. A change here
#: is a format change: a Manifest without the optional keys must serialise
#: byte-identically to one written before they existed.
GOLDEN = """{
  "format_version": 1,
  "created_at": "2026-10-27T09:15:00Z",
  "backup_scope": "all",
  "source": {
    "site_root": "/srv/www",
    "db_host": "db",
    "db_port": 3306,
    "db_name": "wordpress",
    "table_prefix": "wp_",
    "site_url": null,
    "home_url": null
  },
  "encrypted": false,
  "payload": {
    "name": "payload.tar.gz",
    "size": 10,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "members": [
    {
      "path": "dump/database.sql",
      "role": "dump",
      "kind": "file",
      "size": 10,
      "link_target": null
    }
  ],
  "dump_present": true
}
"""


def _minimal() -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at="2026-10-27T09:15:00Z",
        backup_scope="all",
        source=SOURCE,
        encrypted=False,
        payload=ManifestPayload(PAYLOAD_MEMBER, 10, "a" * 64),
        members=(
            ManifestMember(
                member_path(MemberRole.DUMP, "database.sql"),
                MemberRole.DUMP,
                MemberKind.FILE,
                10,
                None,
            ),
        ),
        dump_present=True,
    )


def full_manifest(
    *, encrypted: bool, source: Mapping[str, SourceValue] | None = None
) -> Manifest:
    members = (
        ManifestMember("dump/database.sql", MemberRole.DUMP, MemberKind.FILE, 10, None),
        ManifestMember("site-root", MemberRole.SITE_ROOT, MemberKind.DIR, 0, None),
        ManifestMember(
            "site-root/empty", MemberRole.SITE_ROOT, MemberKind.DIR, 0, None
        ),
        ManifestMember(
            "site-root/index.php", MemberRole.SITE_ROOT, MemberKind.FILE, 42, None
        ),
        ManifestMember(
            "site-root/latest", MemberRole.SITE_ROOT, MemberKind.SYMLINK, 0, "../x y"
        ),
        ManifestMember(
            "config/wp-config.php", MemberRole.CONFIG, MemberKind.FILE, 900, None
        ),
    )
    name = ENCRYPTED_PAYLOAD_MEMBER if encrypted else PAYLOAD_MEMBER
    return Manifest(
        format_version=FORMAT_VERSION,
        created_at="2026-10-27T09:15:00Z",
        backup_scope="all",
        source=SOURCE if source is None else source,
        encrypted=encrypted,
        payload=ManifestPayload(name, 1234, "0123456789abcdef" * 4),
        members=members,
        dump_present=True,
        origin=ManifestOrigin(OriginKind.SAFETY_SNAPSHOT, "overwrite", "run-1"),
        exclusions=ManifestExclusions(
            source="custom",
            patterns=("wp-content/cache/",),
            excluded=(ExcludedEntry("wp-content/cache", "wp-content/cache/", True),),
        ),
        config_path="../wp-config.php",
    )


# --------------------------------------------------------------------------
# member_path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "relative", "expected"),
    [
        (MemberRole.DUMP, "database.sql", "dump/database.sql"),
        (MemberRole.SITE_ROOT, "wp-content/a b.txt", "site-root/wp-content/a b.txt"),
        (MemberRole.SITE_ROOT, "", "site-root"),
        (MemberRole.CONFIG, "wp-config.php", "config/wp-config.php"),
    ],
)
def test_member_path_prefixes_the_role(
    role: MemberRole, relative: str, expected: str
) -> None:
    assert member_path(role, relative) == expected


@pytest.mark.parametrize(
    "relative",
    [
        "/etc/passwd",
        "..",
        "../escape",
        "a/../../escape",
        "a/..",
        "a\x00b",
        "a\\..\\b",
        "a//b",
        "a/",
        "./a",
    ],
)
def test_member_path_refuses_an_escaping_path(relative: str) -> None:
    with pytest.raises(UnsafeMemberPathError):
        member_path(MemberRole.SITE_ROOT, relative)


# --------------------------------------------------------------------------
# The Manifest
# --------------------------------------------------------------------------


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_full_manifest_round_trips(encrypted: bool) -> None:
    manifest = full_manifest(encrypted=encrypted)
    assert manifest_from_json(manifest_to_json(manifest)) == manifest
    assert {m.kind for m in manifest.members} == set(MemberKind)


def test_the_optional_keys_round_trip() -> None:
    back = manifest_from_json(manifest_to_json(full_manifest(encrypted=False)))
    assert back.origin == ManifestOrigin(
        OriginKind.SAFETY_SNAPSHOT, "overwrite", "run-1"
    )
    assert back.exclusions is not None
    assert back.exclusions.excluded[0].is_dir is True
    assert back.config_path == "../wp-config.php"


def test_a_non_utf8_file_name_round_trips_through_utf8_bytes() -> None:
    # A Latin-1 name reaches Python as a lone surrogate; the Manifest must
    # still encode as UTF-8 and read back to the identical string.
    path = os.fsdecode(b"site-root/caf\xe9.jpg")
    member = ManifestMember(path, MemberRole.SITE_ROOT, MemberKind.FILE, 3, None)
    manifest = dataclasses.replace(_minimal(), members=(member,))
    encoded = manifest_to_json(manifest).encode("utf-8")
    back = manifest_from_json(encoded.decode("utf-8"))
    assert back.members[0].path == path
    assert back == manifest


def test_a_manifest_without_optional_keys_matches_the_golden_bytes() -> None:
    assert manifest_to_json(_minimal()) == GOLDEN
    assert manifest_from_json(GOLDEN) == _minimal()
    document = json.loads(GOLDEN)
    assert not {"origin", "exclusions", "config_path"} & document.keys()


def test_unknown_keys_are_ignored() -> None:
    document = json.loads(GOLDEN)
    document["from_the_future"] = {"x": 1}
    document["payload"]["extra"] = True
    document["members"][0]["mode"] = 420
    assert manifest_from_json(json.dumps(document)) == _minimal()


def test_an_unknown_origin_kind_reads_as_none_and_is_reported() -> None:
    document = json.loads(manifest_to_json(full_manifest(encrypted=False)))
    document["origin"]["kind"] = "scheduled_rotation"
    reported: list[tuple[str, str]] = []
    back = manifest_from_json(
        json.dumps(document), on_unknown=lambda f, v: reported.append((f, v))
    )
    assert back.origin is None
    assert reported == [("origin.kind", "scheduled_rotation")]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["members"][0].__setitem__("kind", "hardlink"),
        lambda d: d["members"][0].__setitem__("role", "wp-root"),
        lambda d: d.__setitem__("origin", "safety_snapshot"),
        lambda d: d.__setitem__("origin", {"kind": "safety_snapshot"}),
        lambda d: d.__setitem__("encrypted", "no"),
        lambda d: d.__setitem__("format_version", True),
        lambda d: d["payload"].__setitem__("sha256", "A" * 64),
        lambda d: d["payload"].__setitem__("name", "payload.zip"),
        lambda d: d["source"].__setitem__("db_port", 3306.0),
        lambda d: d.__delitem__("members"),
        lambda d: d["members"][0].__setitem__("size", -1),
        lambda d: d["members"][0].__setitem__("link_target", "x"),
    ],
)
def test_a_malformed_field_is_refused(mutate: object) -> None:
    document = json.loads(GOLDEN)
    mutate(document)  # type: ignore[operator]
    with pytest.raises(MalformedManifestError):
        manifest_from_json(json.dumps(document))


def test_text_that_is_not_json_is_malformed() -> None:
    with pytest.raises(MalformedManifestError) as caught:
        manifest_from_json("{not json")
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_the_manifest_is_deeply_immutable() -> None:
    manifest = _minimal()
    with pytest.raises(TypeError):
        manifest.source["db_name"] = "other"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.encrypted = True  # type: ignore[misc]


def test_an_index_entry_has_no_checksum_or_encrypted_flag() -> None:
    names = {f.name for f in dataclasses.fields(ManifestMember)}
    assert names == {"path", "role", "kind", "size", "link_target"}


def test_no_key_material_field_exists() -> None:
    forbidden = ("key", "kdf", "salt", "nonce", "recipient", "identity", "passphrase")
    for cls in (Manifest, ManifestPayload, ManifestOrigin, ManifestExclusions):
        for f in dataclasses.fields(cls):
            assert not any(word in f.name for word in forbidden), (cls, f.name)
    document = json.loads(manifest_to_json(full_manifest(encrypted=True)))
    assert not any(word in key for key in document for word in forbidden)


def test_the_reserved_source_keys_are_absent() -> None:
    descriptor = SiteDescriptor(
        PurePosixPath("/srv/www"), "db", 3306, "wordpress", "wp_", None, None, "x"
    )
    source = source_from_descriptor(descriptor)
    assert not set(RESERVED_SOURCE_KEYS) & source.keys()


def test_database_still_exposes_no_wordpress_options_read() -> None:
    """When database ships a read of ``siteurl``/``home`` as the WordPress
    account, this fails, so the reserved ``source`` keys get written."""
    database = pytest.importorskip("l3io.wp.database")
    words = ("option", "siteurl", "site_url", "home")
    offenders = [n for n in database.__all__ if any(w in n.lower() for w in words)]
    survey_fields = {f.name for f in dataclasses.fields(database.Survey)}
    offenders += [f for f in survey_fields if any(w in f.lower() for w in words)]
    assert offenders == [], f"write stored_siteurl/stored_home now: {offenders}"


@pytest.mark.parametrize(
    ("path", "role", "safe"),
    [
        ("config/wp-config.php", MemberRole.CONFIG, True),
        ("dump/database.sql", MemberRole.DUMP, True),
        ("site-root/wp-content/a.php", MemberRole.SITE_ROOT, True),
        ("config/.ssh/authorized_keys", MemberRole.CONFIG, False),
        ("config/other.php", MemberRole.CONFIG, False),
        ("dump/x/y.sql", MemberRole.DUMP, False),
        ("dump/archive.bin", MemberRole.DUMP, False),
    ],
)
def test_config_and_dump_roles_hold_only_their_one_member(
    path: str, role: MemberRole, safe: bool
) -> None:
    """EP-RT-002: the builder writes exactly these two names, so nothing else
    under ``config`` or ``dump`` may be extracted into a directory the
    Archive does not own."""
    assert is_safe_member_path(path, role) is safe


# --------------------------------------------------------------------------
# The inner Manifest (BL-E002-115, ADR-0024 amendment 2026-11-06)
# --------------------------------------------------------------------------


def _other() -> Manifest:
    """A Manifest that differs from ``full_manifest(encrypted=True)`` in every field."""
    base = full_manifest(encrypted=True)
    return Manifest(
        format_version=FORMAT_VERSION + 1,
        created_at="2026-11-06T00:00:00Z",
        backup_scope="files",
        source={**SOURCE, "db_name": "elsewhere"},
        encrypted=False,
        payload=ManifestPayload(PAYLOAD_MEMBER, 1, "f" * 64),
        members=base.members[:-1],
        dump_present=False,
        origin=ManifestOrigin(OriginKind.SAFETY_SNAPSHOT, "overwrite", "run-2"),
        exclusions=ManifestExclusions(source="default", patterns=(), excluded=()),
        config_path="wp-config.php",
    )


def test_a_matching_pair_has_no_difference() -> None:
    manifest = full_manifest(encrypted=True)
    inner = inner_manifest_bytes(manifest)
    assert check_inner_manifest(manifest_to_json(manifest), inner) is None


def test_the_inner_manifest_leaves_out_only_the_payloads_size_and_sha256() -> None:
    manifest = full_manifest(encrypted=True)
    inner = json.loads(inner_manifest_bytes(manifest))
    outer = json.loads(manifest_to_json(manifest))
    assert inner["payload"] == {"name": ENCRYPTED_PAYLOAD_MEMBER}
    del outer["payload"]["size"], outer["payload"]["sha256"]
    assert inner == outer
    # The encrypted payload's own size and sha256 are what the outer one adds.
    resized = dataclasses.replace(
        manifest, payload=ManifestPayload(ENCRYPTED_PAYLOAD_MEMBER, 99, "b" * 64)
    )
    assert (
        check_inner_manifest(manifest_to_json(resized), inner_manifest_bytes(manifest))
        is None
    )


@pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(Manifest)])
def test_each_field_changed_outside_the_payload_is_named(name: str) -> None:
    """The field set is ``Manifest``'s own, not a list typed here."""
    base, other = full_manifest(encrypted=True), _other()
    assert getattr(base, name) != getattr(other, name), f"_other() must change {name}"
    edited = dataclasses.replace(base, **{name: getattr(other, name)})
    found = check_inner_manifest(manifest_to_json(edited), inner_manifest_bytes(base))
    assert found is not None
    assert found == name or found.startswith((f"{name}.", f"{name}["))


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            lambda d: d["source"].update(site_url="https://evil.example"),
            "source.site_url",
        ),
        (lambda d: d["members"][3].update(size=43), "members[3].size"),
        (lambda d: d["members"][1].update(path="site-root/x"), "members[1].path"),
        (lambda d: d["source"].pop("db_host"), "source"),
        (lambda d: d["source"].update({"\x1b[2J evil": 1}), "source"),
        (lambda d: d["exclusions"]["patterns"].append("x/"), "exclusions.patterns"),
    ],
)
def test_the_first_difference_is_a_path_never_a_value(
    edit: object, expected: str
) -> None:
    manifest = full_manifest(encrypted=True)
    document = json.loads(manifest_to_json(manifest))
    edit(document)  # type: ignore[operator]
    found = check_inner_manifest(json.dumps(document), inner_manifest_bytes(manifest))
    assert found == expected
    assert "evil" not in str(found)


@pytest.mark.parametrize(
    "inner",
    [b"not json", b"\xff\xfe", b"{}", b'{"payload": {"name": "payload.tar.gz.age"}}'],
)
def test_an_unparsable_inner_manifest_is_named_so(inner: bytes) -> None:
    outer = manifest_to_json(full_manifest(encrypted=True))
    assert check_inner_manifest(outer, inner) == INNER_UNPARSABLE


def test_the_owner_guard_reaches_the_inner_manifest_name(tmp_path: Path) -> None:
    """N-8: a second spelling of the reserved name is caught by the AD-6 guard."""
    from tests.contracts.test_archive_format_owner import _offenders

    assert INNER_MANIFEST_MEMBER == "inner-manifest.json"
    planted = tmp_path / "leak.py"
    planted.write_text("NAME = 'inner-manifest.json'\n", encoding="utf-8")
    owner = tmp_path / "owner.py"
    owner.write_text("", encoding="utf-8")
    assert _offenders([planted, owner], owner) == ["leak.py:1: 'inner-manifest.json'"]
