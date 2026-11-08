"""AD-23/ADR-0024 — the pure verification comparison.

One case per :class:`VerificationReason`, keyed by the enum; the case table's
scope is the enum itself, so a reason added without a case fails
``test_every_reason_has_a_case``. Each case is probe facts built by hand and
driven through ``verify_probe`` (classify, then ``verify``).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest

from l3io.wp.backup.core.verify import verify, verify_probe
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    V0_DUMP_MEMBER,
    V0_SITE_ROOT,
    Integrity,
    MemberDigest,
    MemberIndex,
    MemberKind,
    TarMemberType,
    VerificationReason,
    classify,
)

DIGEST = "ab" * 32


def manifest_text(**changes: object) -> str:
    """A v1 Manifest whose payload is :data:`PAYLOAD_DIGEST`."""
    document: dict[str, object] = {
        "format_version": 1,
        "created_at": "2026-11-02T00:00:00+00:00",
        "backup_scope": "all",
        "source": {},
        "encrypted": False,
        "payload": {"name": PAYLOAD_MEMBER, "size": 10, "sha256": DIGEST},
        "members": [
            entry("dump/database.sql", "dump"),
            entry("site-root", "site-root", "dir"),
            entry("site-root/latest.php", "site-root", "symlink", "index.php"),
        ],
        "dump_present": True,
    }
    document.update(changes)
    return json.dumps(document)


R = VerificationReason


def entry(
    path: str, role: str, kind: str = "file", target: str | None = None
) -> dict[str, object]:
    return {"path": path, "role": role, "kind": kind, "size": 0, "link_target": target}


Facts = tuple[MemberIndex, dict[str, MemberDigest]]

MANIFEST_DIGEST = MemberDigest(TarMemberType.FILE, 100, "cd" * 32, None)
PAYLOAD_DIGEST = MemberDigest(TarMemberType.FILE, 10, DIGEST, None)


def good(text: str | None = None, **digests: MemberDigest) -> Facts:
    names = (MANIFEST_MEMBER, PAYLOAD_MEMBER, *digests)
    index = MemberIndex(
        names=names,
        first_member=MANIFEST_MEMBER,
        manifest_text=manifest_text() if text is None else text,
        readable_to_end=True,
        gzip=False,
    )
    found = {MANIFEST_MEMBER: MANIFEST_DIGEST, PAYLOAD_MEMBER: PAYLOAD_DIGEST}
    return index, {**found, **digests}


def payload(**changes: object) -> Facts:
    index, digests = good()
    digests[PAYLOAD_MEMBER] = dataclasses.replace(PAYLOAD_DIGEST, **changes)  # type: ignore[arg-type]
    return index, digests


def names(*found: str, readable: bool = True, gzip: bool = False) -> Facts:
    index = MemberIndex(
        names=found,
        first_member=found[0] if found else None,
        manifest_text=manifest_text() if found[:1] == (MANIFEST_MEMBER,) else None,
        readable_to_end=readable,
        gzip=gzip,
    )
    return index, dict.fromkeys(found, PAYLOAD_DIGEST)


def v0_hard_link() -> Facts:
    index, _ = names(V0_SITE_ROOT, f"{V0_SITE_ROOT}/x")
    link = MemberDigest(TarMemberType.HARDLINK, 0, None, V0_SITE_ROOT)
    return index, {f"{V0_SITE_ROOT}/x": link}


CASES: dict[VerificationReason, tuple[Callable[[], Facts], str | None]] = {
    R.CHECKSUM_MISMATCH: (lambda: payload(sha256="00" * 32), PAYLOAD_MEMBER),
    R.SIZE_MISMATCH: (lambda: payload(size=11), PAYLOAD_MEMBER),
    R.MEMBER_MISSING: (lambda: names(MANIFEST_MEMBER), PAYLOAD_MEMBER),
    R.UNDECLARED_MEMBER: (
        lambda: good(extra=PAYLOAD_DIGEST),
        "extra",
    ),
    R.KIND_MISMATCH: (
        lambda: payload(tar_type=TarMemberType.SYMLINK, sha256=None),
        PAYLOAD_MEMBER,
    ),
    R.UNSUPPORTED_MEMBER_TYPE: (v0_hard_link, f"{V0_SITE_ROOT}/x"),
    R.DUMP_DECLARED_ABSENT: (
        lambda: good(manifest_text(members=[entry("site-root", "site-root", "dir")])),
        None,
    ),
    R.TRUNCATED: (lambda: names(MANIFEST_MEMBER, readable=False), None),
    R.UNREADABLE: (lambda: good("{not json"), MANIFEST_MEMBER),
    R.UNRECOGNISED_FORMAT: (lambda: names(V0_DUMP_MEMBER), V0_DUMP_MEMBER),
    R.UNSUPPORTED_VERSION: (lambda: good(json.dumps({"format_version": 7})), "7"),
    R.UNSAFE_MEMBER_PATH: (
        lambda: good(
            manifest_text(
                dump_present=False,
                members=[entry("site-root/../../etc/passwd", "site-root")],
            )
        ),
        "site-root/../../etc/passwd",
    ),
}


def test_every_reason_has_a_case() -> None:
    assert set(CASES) == set(VerificationReason)


@pytest.mark.parametrize("reason", list(VerificationReason), ids=str)
def test_each_reason(reason: VerificationReason) -> None:
    build, member = CASES[reason]
    _, result = verify_probe(*build())
    assert result.passed is False
    assert (member, reason) in [(f.member, f.reason) for f in result.failures]


def test_a_good_v1_passes_verified() -> None:
    view, result = verify_probe(*good())
    assert view is not None
    assert (result.passed, result.integrity, result.format_version) == (
        True,
        Integrity.VERIFIED,
        1,
    )
    assert result.failures == ()


def test_a_good_v0_passes_structural_only() -> None:
    view, result = verify_probe(*names(V0_SITE_ROOT, V0_DUMP_MEMBER, gzip=True))
    assert view is not None
    assert view.dump_present is True
    assert (result.passed, result.integrity, result.format_version) == (
        True,
        Integrity.STRUCTURAL_ONLY,
        None,
    )


def test_a_v1_archive_cannot_pass_as_structural_only() -> None:
    """AG-17: the view a v1 Archive classifies to is VERIFIED, pass or fail."""
    index, digests = good()
    view = classify(index)
    downgraded = dataclasses.replace(view, integrity=Integrity.STRUCTURAL_ONLY)
    assert verify(view, digests, index).integrity is Integrity.VERIFIED
    assert verify(downgraded, digests, index).integrity is Integrity.STRUCTURAL_ONLY
    assert view.integrity is Integrity.VERIFIED


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (lambda: good(manifest_text(), extra2=PAYLOAD_DIGEST), "extra2"),
        (
            lambda: (
                dataclasses.replace(
                    good()[0], names=(MANIFEST_MEMBER, PAYLOAD_MEMBER, MANIFEST_MEMBER)
                ),
                good()[1],
            ),
            MANIFEST_MEMBER,
        ),
    ],
)
def test_a_second_manifest_or_extra_member_is_undeclared(
    facts: Callable[[], Facts], expected: str
) -> None:
    _, result = verify_probe(*facts())
    assert (expected, R.UNDECLARED_MEMBER) in [
        (f.member, f.reason) for f in result.failures
    ]


@pytest.mark.parametrize("wrapped", [True, False], ids=["gzip", "plain"])
def test_both_outer_encodings_are_v1(*, wrapped: bool) -> None:
    """Owner decision 21: gzip-wrapped (new) and plain (earlier) outer tars."""
    index, digests = good()
    _, result = verify_probe(dataclasses.replace(index, gzip=wrapped), digests)
    assert result.passed, result.failures


def test_a_payload_name_disagreeing_with_encrypted_is_unreadable() -> None:
    text = manifest_text(encrypted=True)
    _, result = verify_probe(*good(text))
    assert (PAYLOAD_MEMBER, R.UNREADABLE) in [
        (f.member, f.reason) for f in result.failures
    ]


def test_a_duplicate_index_path_is_unreadable() -> None:
    dump = entry("dump/database.sql", "dump")
    _, result = verify_probe(*good(manifest_text(members=[dump, dump])))
    assert [(f.member, f.reason) for f in result.failures] == [
        ("dump/database.sql", R.UNREADABLE)
    ]


def test_a_link_target_on_a_file_is_unreadable() -> None:
    index, digests = good()
    view = classify(index)
    bad = dataclasses.replace(view.members[0], link_target="x", kind=MemberKind.FILE)
    view = dataclasses.replace(view, members=(bad, *view.members[1:]))
    assert (bad.path, R.UNREADABLE) in [
        (f.member, f.reason) for f in verify(view, digests, index).failures
    ]
