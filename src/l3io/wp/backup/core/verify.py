"""AD-23/ADR-0024 — verification: a pure comparison of what a probe read.

:func:`verify` compares an :class:`ArchiveView` with the outer members'
digests and the member index; :func:`verify_probe` classifies first and turns
the classifier's refusals into failures, so every :class:`VerificationReason`
is reachable from probe facts alone. Nothing here reads a file.

**v1 (keyless).** The Manifest member occurs once, first; the only other outer
member is the payload the Manifest names, a regular file whose size and
SHA-256 equal the Manifest's; the outer tar is not compressed; the member
index is well formed (every path within its role prefix, none twice, a
``link_target`` only on a symlink) and a declared Dump has a ``dump``-role
entry. The inner tar is not opened: per-file checksums are withdrawn
(ADR-0024). **v0.** The stream reads to its end, one of the two v0 shapes
holds, and no member is a hard link or a special file. A v1 pass is
``VERIFIED``, a v0 pass ``STRUCTURAL_ONLY``; a result whose Archive could not
be classified carries ``STRUCTURAL_ONLY`` and ``format_version=None``, since
nothing was verified.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from l3io.wp.backup.domain.archive_format import (
    ENCRYPTED_PAYLOAD_MEMBER,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ArchiveView,
    Integrity,
    ManifestPayload,
    MemberDigest,
    MemberIndex,
    MemberKind,
    MemberRole,
    TarMemberType,
    VerificationFailure,
    VerificationReason,
    VerificationResult,
    classify,
    is_safe_member_path,
    member_relative,
)
from l3io.wp.backup.domain.errors import (
    MalformedManifestError,
    UnrecognisedArchiveError,
    UnsupportedFormatVersionError,
)

__all__ = ["verify", "verify_probe"]

_R = VerificationReason
_UNSUPPORTED_TYPES = frozenset({TarMemberType.HARDLINK, TarMemberType.OTHER})


def _fail(member: str | None, reason: VerificationReason) -> VerificationFailure:
    return VerificationFailure(member=member, reason=reason)


def _outer_types(digests: Mapping[str, MemberDigest]) -> list[VerificationFailure]:
    return [
        _fail(name, _R.UNSUPPORTED_MEMBER_TYPE)
        for name, digest in digests.items()
        if digest.tar_type in _UNSUPPORTED_TYPES
    ]


def _outer_members(
    payload: ManifestPayload, index: MemberIndex
) -> list[VerificationFailure]:
    failures = []
    seen: Counter[str] = Counter()
    for position, name in enumerate(index.names):
        seen[name] += 1
        expected = MANIFEST_MEMBER if position == 0 else payload.name
        if name != expected or seen[name] > 1:
            failures.append(_fail(name, _R.UNDECLARED_MEMBER))
    if payload.name not in seen:
        failures.append(_fail(payload.name, _R.MEMBER_MISSING))
    return failures


def _payload(
    view: ArchiveView, payload: ManifestPayload, digests: Mapping[str, MemberDigest]
) -> list[VerificationFailure]:
    expected = ENCRYPTED_PAYLOAD_MEMBER if view.encrypted else PAYLOAD_MEMBER
    if payload.name != expected:
        return [_fail(payload.name, _R.UNREADABLE)]
    digest = digests.get(payload.name)
    if digest is None or digest.tar_type in _UNSUPPORTED_TYPES:
        return []  # MEMBER_MISSING / UNSUPPORTED_MEMBER_TYPE already say so
    if digest.tar_type is not TarMemberType.FILE:
        return [_fail(payload.name, _R.KIND_MISMATCH)]
    if digest.size != payload.size:
        return [_fail(payload.name, _R.SIZE_MISMATCH)]
    if digest.sha256 != payload.sha256:
        return [_fail(payload.name, _R.CHECKSUM_MISMATCH)]
    return []


def _member_index(view: ArchiveView) -> list[VerificationFailure]:
    failures = []
    seen: set[str] = set()
    for member in view.members:
        if not is_safe_member_path(member.path, member.role):
            failures.append(_fail(member.path, _R.UNSAFE_MEMBER_PATH))
        if member.path in seen:
            failures.append(_fail(member.path, _R.UNREADABLE))
        seen.add(member.path)
        if (member.link_target is not None) != (member.kind is MemberKind.SYMLINK):
            failures.append(_fail(member.path, _R.UNREADABLE))
    if view.dump_present and not any(m.role is MemberRole.DUMP for m in view.members):
        failures.append(_fail(None, _R.DUMP_DECLARED_ABSENT))
    return failures


def _v1(
    view: ArchiveView,
    payload: ManifestPayload,
    digests: Mapping[str, MemberDigest],
    index: MemberIndex,
) -> list[VerificationFailure]:
    failures = []
    if index.gzip:
        failures.append(_fail(None, _R.UNRECOGNISED_FORMAT))
    failures += _outer_members(payload, index)
    failures += _outer_types(digests)
    failures += _payload(view, payload, digests)
    failures += _member_index(view)
    return failures


def _v0(
    view: ArchiveView, digests: Mapping[str, MemberDigest], index: MemberIndex
) -> list[VerificationFailure]:
    failures = [
        _fail(m.path, _R.UNSAFE_MEMBER_PATH)
        for m in view.members
        if member_relative(view, m.path) is None
    ]
    failures += _outer_types(digests)
    if MANIFEST_MEMBER in index.names:
        failures.append(_fail(MANIFEST_MEMBER, _R.UNRECOGNISED_FORMAT))
    return failures


def verify(
    view: ArchiveView, digests: Mapping[str, MemberDigest], index: MemberIndex
) -> VerificationResult:
    """Compare ``view`` with what the probe streamed. Never raises for an Archive."""
    if not index.readable_to_end:
        failures = [_fail(None, _R.TRUNCATED)]
    elif view.payload is None:
        failures = _v0(view, digests, index)
    else:
        failures = _v1(view, view.payload, digests, index)
    return VerificationResult(
        passed=not failures,
        integrity=view.integrity,
        format_version=view.format_version,
        failures=tuple(failures),
    )


def _unclassified(failure: VerificationFailure) -> VerificationResult:
    return VerificationResult(
        passed=False,
        integrity=Integrity.STRUCTURAL_ONLY,
        format_version=None,
        failures=(failure,),
    )


def verify_probe(
    index: MemberIndex, digests: Mapping[str, MemberDigest]
) -> tuple[ArchiveView | None, VerificationResult]:
    """Classify the probe's facts, then :func:`verify` them.

    Returns the view (``None`` when the Archive could not be classified) and
    the result.

    A stream that did not read to its end is ``TRUNCATED`` before anything is
    classified; the classifier's refusals become ``UNRECOGNISED_FORMAT``
    (naming the first member found), ``UNSUPPORTED_VERSION`` (naming the
    version) and ``UNREADABLE`` (the Manifest).
    """
    if not index.readable_to_end:
        return None, _unclassified(_fail(None, _R.TRUNCATED))
    try:
        view = classify(index)
    except UnrecognisedArchiveError as exc:
        first = exc.found[0] if exc.found else None
        return None, _unclassified(_fail(first, _R.UNRECOGNISED_FORMAT))
    except UnsupportedFormatVersionError as exc:
        return None, _unclassified(_fail(str(exc.found), _R.UNSUPPORTED_VERSION))
    except MalformedManifestError:
        return None, _unclassified(_fail(MANIFEST_MEMBER, _R.UNREADABLE))
    return view, verify(view, digests, index)
