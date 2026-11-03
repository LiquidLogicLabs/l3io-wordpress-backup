"""AD-23 — ``classify`` recognises exactly three shapes. Pure: hand-built indexes."""

from __future__ import annotations

import json

import pytest

from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    SUPPORTED_FORMAT_VERSIONS,
    V0_DUMP_MEMBER,
    V0_SITE_ROOT,
    Integrity,
    ManifestPayload,
    MemberKind,
    MemberRole,
    classify,
    format_label,
    member_relative,
)
from l3io.wp.backup.domain.archive_format import MemberIndex as Index
from l3io.wp.backup.domain.errors import (
    MalformedManifestError,
    UnrecognisedArchiveError,
    UnsupportedFormatVersionError,
)

DIGEST = "ab" * 32


def manifest_text(**changes: object) -> str:
    document: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "created_at": "2026-11-02T00:00:00+00:00",
        "backup_scope": "all",
        "source": {"db_name": "wptest"},
        "encrypted": False,
        "payload": {"name": PAYLOAD_MEMBER, "size": 10, "sha256": DIGEST},
        "members": [
            {
                "path": "dump/database.sql",
                "role": "dump",
                "kind": "file",
                "size": 3,
                "link_target": None,
            },
            {
                "path": "site-root",
                "role": "site-root",
                "kind": "dir",
                "size": 0,
                "link_target": None,
            },
            {
                "path": "site-root/latest.php",
                "role": "site-root",
                "kind": "symlink",
                "size": 0,
                "link_target": "index.php",
            },
        ],
        "dump_present": True,
    }
    document.update(changes)
    return json.dumps({k: v for k, v in document.items() if v is not ...})


def v1(text: str | None = None, *, names: tuple[str, ...] = ()) -> Index:
    names = names or (MANIFEST_MEMBER, PAYLOAD_MEMBER)
    return Index(
        names=names,
        first_member=names[0],
        manifest_text=manifest_text() if text is None else text,
        readable_to_end=True,
        gzip=False,
    )


def v0(*names: str) -> Index:
    return Index(
        names=names,
        first_member=names[0] if names else None,
        manifest_text=None,
        readable_to_end=True,
        gzip=True,
    )


def test_v1_is_verified_capable_with_encrypted_and_payload_copied() -> None:
    view = classify(v1())
    assert view.format_version == FORMAT_VERSION
    assert view.integrity is Integrity.VERIFIED
    assert (view.encrypted, view.payload) == (
        False,
        ManifestPayload(PAYLOAD_MEMBER, 10, DIGEST),
    )
    assert view.dump_present is True
    link = view.members[-1]
    assert (link.kind, link.link_target) == (MemberKind.SYMLINK, "index.php")
    sealed = classify(
        v1(
            manifest_text(
                encrypted=True,
                payload={"name": ENCRYPTED_PAYLOAD_MEMBER, "size": 1, "sha256": DIGEST},
            )
        )
    )
    assert sealed.encrypted is True
    assert sealed.payload is not None
    assert sealed.payload.name == ENCRYPTED_PAYLOAD_MEMBER


@pytest.mark.parametrize(
    ("value", "expected"),
    [("../wp-config.php", "../wp-config.php"), (..., None), ("/etc/x", "/etc/x")],
)
def test_v1_config_path_is_copied_as_found(value: object, expected: object) -> None:
    assert classify(v1(manifest_text(config_path=value))).config_path == expected


def test_v0_with_a_dump() -> None:
    view = classify(v0(V0_SITE_ROOT, V0_DUMP_MEMBER, f"{V0_SITE_ROOT}/wp-config.php"))
    assert (view.format_version, view.integrity) == (None, Integrity.STRUCTURAL_ONLY)
    assert view.dump_present is True
    assert [(m.path, m.role, m.kind) for m in view.members] == [
        (V0_SITE_ROOT, MemberRole.SITE_ROOT, MemberKind.DIR),
        (V0_DUMP_MEMBER, MemberRole.DUMP, MemberKind.FILE),
        (f"{V0_SITE_ROOT}/wp-config.php", MemberRole.SITE_ROOT, MemberKind.FILE),
    ]
    assert view.config_path == f"{V0_SITE_ROOT}/wp-config.php"
    assert (view.encrypted, view.payload) == (False, None)


def test_v0_files_only() -> None:
    view = classify(v0(f"{V0_SITE_ROOT}/index.php"))
    assert view.dump_present is False
    assert MemberRole.DUMP not in {m.role for m in view.members}
    assert view.config_path is None


@pytest.mark.parametrize(
    "names",
    [
        (V0_DUMP_MEMBER,),
        (f"{V0_SITE_ROOT}/a", MANIFEST_MEMBER),
        (f"{V0_SITE_ROOT}/a", "stray.txt"),
        ("wp-rootish/a",),
        (),
    ],
)
def test_anything_else_is_unrecognised(names: tuple[str, ...]) -> None:
    with pytest.raises(UnrecognisedArchiveError) as caught:
        classify(v0(*names))
    assert caught.value.found == names[:5]


def test_a_manifest_not_first_is_unrecognised() -> None:
    with pytest.raises(UnrecognisedArchiveError):
        classify(v1(names=(PAYLOAD_MEMBER, MANIFEST_MEMBER)))


def test_an_unsupported_format_version_names_it() -> None:
    assert 2 not in SUPPORTED_FORMAT_VERSIONS
    with pytest.raises(UnsupportedFormatVersionError) as caught:
        classify(v1(json.dumps({"format_version": 2, "anything": "else"})))
    assert caught.value.found == 2


@pytest.mark.parametrize(
    "text", [None, "{not json", json.dumps({"no": "version"}), manifest_text(members=1)]
)
def test_an_unreadable_manifest_is_malformed(text: str | None) -> None:
    index = Index(
        names=(MANIFEST_MEMBER, PAYLOAD_MEMBER),
        first_member=MANIFEST_MEMBER,
        manifest_text=text,
        readable_to_end=True,
        gzip=False,
    )
    with pytest.raises(MalformedManifestError):
        classify(index)


def test_member_relative_strips_each_format_s_prefix() -> None:
    view1 = classify(v1())
    assert member_relative(view1, "site-root/a/b.php") == (
        MemberRole.SITE_ROOT,
        "a/b.php",
    )
    assert member_relative(view1, "site-root") == (MemberRole.SITE_ROOT, "")
    assert member_relative(view1, "site-root/../x") is None
    assert member_relative(view1, "elsewhere/x") is None
    view0 = classify(v0(V0_DUMP_MEMBER, f"{V0_SITE_ROOT}/a"))
    assert member_relative(view0, V0_DUMP_MEMBER) == (MemberRole.DUMP, "database.sql")
    assert member_relative(view0, f"{V0_SITE_ROOT}/a") == (MemberRole.SITE_ROOT, "a")
    assert member_relative(view0, "site-root/a") is None


def test_format_label() -> None:
    assert (format_label(None), format_label(1)) == ("v0", 1)
