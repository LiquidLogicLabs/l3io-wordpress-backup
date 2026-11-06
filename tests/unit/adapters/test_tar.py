"""BL-E002-116, AD-11 — extraction enforces the indexed sizes (E003-S01-001).

A crafted v1 Archive whose payload header states more bytes than its member
index declares is refused during extraction; :func:`require_patched_tarfile`
is the one interpreter capability check, shared by ``extract_archive`` and
the restore's ``PREFLIGHT``.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters import tar as tar_module
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    extract_archive,
    require_patched_tarfile,
)
from l3io.wp.backup.domain.archive_format import (
    INNER_MANIFEST_MEMBER,
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ArchiveView,
    Integrity,
    ManifestPayload,
    MemberKind,
    MemberRole,
    ViewMember,
)
from l3io.wp.backup.domain.errors import ExtractionRefusedError, MemberReadRefusedError
from l3io.wp.backup.domain.storage_target import StorageTarget

SITE = frozenset({MemberRole.SITE_ROOT})
BODY = b"<?php // twelve-plus bytes of body\n"


def _crafted(
    tmp_path: Path, declared: dict[str, int]
) -> tuple[StorageTarget, ArchiveView]:
    """A v1 Archive of two files whose index declares ``declared`` sizes."""
    plain = io.BytesIO()
    names = ("site-root/a.php", "site-root/b.php")
    with tarfile.open(fileobj=plain, mode="w:gz") as inner:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = len(BODY)
            inner.addfile(info, io.BytesIO(BODY))
    payload = plain.getvalue()
    path = tmp_path / "crafted.tar.gz"
    with tarfile.open(path, mode="w") as outer:
        for name, data in ((MANIFEST_MEMBER, b"{}"), (PAYLOAD_MEMBER, payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            outer.addfile(info, io.BytesIO(data))
    view = ArchiveView(
        format_version=1,
        members=tuple(
            ViewMember(n, MemberRole.SITE_ROOT, MemberKind.FILE, None, declared[n])
            for n in names
        ),
        dump_present=False,
        config_path=None,
        integrity=Integrity.VERIFIED,
        encrypted=False,
        payload=ManifestPayload(
            name=PAYLOAD_MEMBER,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        backup_scope="files",
    )
    return StorageTarget(scheme="file", path=PurePosixPath(path)), view


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_members_at_their_indexed_size_extract(
    tmp_path: Path, destination: Path
) -> None:
    exact = {"site-root/a.php": len(BODY), "site-root/b.php": len(BODY)}
    source, view = _crafted(tmp_path, exact)
    result = extract_archive(source, destination, view, roles=SITE)
    assert result.bytes == 2 * len(BODY)
    assert (destination / "b.php").read_bytes() == BODY


def test_a_member_larger_than_its_index_entry_is_refused(
    tmp_path: Path, destination: Path
) -> None:
    short = {"site-root/a.php": len(BODY), "site-root/b.php": len(BODY) - 1}
    source, view = _crafted(tmp_path, short)
    with pytest.raises(
        ExtractionRefusedError, match="the archive lists for it"
    ) as caught:
        extract_archive(source, destination, view, roles=SITE)
    assert caught.value.member == "site-root/b.php"
    assert not (destination / "b.php").exists()


def test_the_running_total_stops_at_the_indexed_sum(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the per-member check defeated, the total cap still refuses."""
    exact = {"site-root/a.php": len(BODY), "site-root/b.php": len(BODY)}
    source, view = _crafted(tmp_path, exact)
    # The cap is the sum over the roles extracted; shrink it under the filter.
    real = tar_module._ExtractFilter  # noqa: SLF001 - the cap, isolated

    def shrunk(**kwargs: object) -> object:
        kwargs["cap"] = len(BODY) + 1
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tar_module, "_ExtractFilter", shrunk)
    with pytest.raises(ExtractionRefusedError, match="running total"):
        extract_archive(source, destination, view, roles=SITE)


# --------------------------------------------------------------------------
# The inner Manifest's reserved name (BL-E002-115, ADR-0024 amendment)
# --------------------------------------------------------------------------

INDEXED = {"site-root/a.php": len(BODY), "site-root/b.php": len(BODY)}


def _with_extra(
    tmp_path: Path, extra: tarfile.TarInfo, *, encrypted: bool
) -> tuple[StorageTarget, ArchiveView, Path]:
    """``_crafted``'s Archive, its plain payload gaining ``extra`` last.

    The plain payload is handed to the reader as ``payload=``, as
    ``decrypt_payload`` would have written it; ``encrypted`` is the view's.
    """
    source, view = _crafted(tmp_path, INDEXED)
    plain = tmp_path / "plain.tar.gz"
    with tarfile.open(plain, mode="w:gz") as inner:
        for name in INDEXED:
            info = tarfile.TarInfo(name)
            info.size = len(BODY)
            inner.addfile(info, io.BytesIO(BODY))
        inner.addfile(extra, io.BytesIO(b"{}") if extra.isfile() else None)
    return source, dataclasses.replace(view, encrypted=encrypted), plain


def _file(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 2
    return info


def _directory(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


def test_extraction_never_writes_the_inner_manifest(
    tmp_path: Path, destination: Path
) -> None:
    source, view, plain = _with_extra(
        tmp_path, _file(INNER_MANIFEST_MEMBER), encrypted=True
    )
    roles = frozenset(MemberRole)
    result = extract_archive(source, destination, view, roles=roles, payload=plain)
    assert result.members == len(INDEXED)
    assert sorted(p.name for p in destination.iterdir()) == ["a.php", "b.php"]


@pytest.mark.parametrize(
    ("extra", "encrypted"),
    [
        (_file(INNER_MANIFEST_MEMBER), False),
        (_file(f"{INNER_MANIFEST_MEMBER}/below"), True),
        (_directory(INNER_MANIFEST_MEMBER), True),
    ],
    ids=["in-a-plain-payload", "below-the-name", "not-a-file"],
)
def test_extraction_refuses_any_other_member_under_the_reserved_name(
    tmp_path: Path, destination: Path, extra: tarfile.TarInfo, encrypted: bool
) -> None:
    source, view, plain = _with_extra(tmp_path, extra, encrypted=encrypted)
    with pytest.raises(ExtractionRefusedError, match="reserved") as caught:
        extract_archive(source, destination, view, roles=SITE, payload=plain)
    assert caught.value.member == extra.name
    assert not (destination / INNER_MANIFEST_MEMBER).exists()


@pytest.mark.parametrize("name", [INNER_MANIFEST_MEMBER, f"{INNER_MANIFEST_MEMBER}/x"])
def test_read_member_refuses_the_reserved_name(tmp_path: Path, name: str) -> None:
    source, view, plain = _with_extra(
        tmp_path, _file(INNER_MANIFEST_MEMBER), encrypted=True
    )
    listed = dataclasses.replace(
        view,
        members=(
            *view.members,
            ViewMember(name, MemberRole.SITE_ROOT, MemberKind.FILE, None, 2),
        ),
    )
    with pytest.raises(MemberReadRefusedError, match="reserved"):
        TarArchiveProbe().read_member(
            source, listed, name, max_bytes=1024, payload=plain
        )
    body = TarArchiveProbe().read_member(
        source, view, "site-root/a.php", max_bytes=1024, payload=plain
    )
    assert body == BODY


def test_require_patched_tarfile_passes_on_this_interpreter() -> None:
    require_patched_tarfile()


@pytest.mark.parametrize(
    ("module", "attribute", "reason"),
    [
        (tarfile, "data_filter", "no data filter"),
        (os.path, "ALLOW_MISSING", "CVE-2025-4517"),
    ],
)
def test_require_patched_tarfile_refuses_a_missing_capability(
    monkeypatch: pytest.MonkeyPatch, module: object, attribute: str, reason: str
) -> None:
    monkeypatch.delattr(module, attribute, raising=False)
    with pytest.raises(ExtractionRefusedError, match=reason):
        require_patched_tarfile()


PACKAGE = Path(tar_module.__file__).resolve().parents[1]


def _owners(package: Path) -> set[tuple[str, str]]:
    """Every function, anywhere under ``package``, naming ``ALLOW_MISSING``."""
    found = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                named = (
                    isinstance(inner, ast.Constant) and inner.value == "ALLOW_MISSING"
                ) or (
                    isinstance(inner, ast.Attribute) and inner.attr == "ALLOW_MISSING"
                )
                if named:
                    found.add((path.relative_to(package).as_posix(), node.name))
    return found


def test_the_capability_check_exists_once() -> None:
    """The interpreter check lives only in ``require_patched_tarfile`` (no second)."""
    assert _owners(PACKAGE) == {("adapters/tar.py", "require_patched_tarfile")}


def test_the_guard_finds_a_second_check_in_a_new_module(tmp_path: Path) -> None:
    """Scope attack: a check planted in a module the guard was never told about."""
    nested = tmp_path / "core" / "deep"
    nested.mkdir(parents=True)
    (nested / "planted.py").write_text(
        "import os\ndef f():\n    return hasattr(os.path, 'ALLOW_MISSING')\n",
        encoding="utf-8",
    )
    assert _owners(tmp_path) == {("core/deep/planted.py", "f")}


# --------------------------------------------------------------------------
# planned_skipped_links agrees with what extraction skips (F-5)
# --------------------------------------------------------------------------

LINKS = [
    ("site-root/abs", "/etc/passwd"),
    ("site-root/up", "../outside"),
    ("site-root/sub/up2", "../../outside"),
    ("site-root/sub/dotdot", ".."),
    ("site-root/exactly", ".."),
    ("site-root/inside", "a.php"),
    ("site-root/sub/inside", "../a.php"),
]


def _with_links(tmp_path: Path) -> tuple[StorageTarget, ArchiveView]:
    plain = io.BytesIO()
    members = []
    with tarfile.open(fileobj=plain, mode="w:gz") as inner:
        for name, kind in (
            ("site-root", tarfile.DIRTYPE),
            ("site-root/sub", tarfile.DIRTYPE),
        ):
            info = tarfile.TarInfo(name)
            info.type, info.mode = kind, 0o755
            inner.addfile(info)
            members.append(
                ViewMember(name, MemberRole.SITE_ROOT, MemberKind.DIR, None, 0)
            )
        info = tarfile.TarInfo("site-root/a.php")
        info.size = len(BODY)
        inner.addfile(info, io.BytesIO(BODY))
        members.append(
            ViewMember(
                "site-root/a.php",
                MemberRole.SITE_ROOT,
                MemberKind.FILE,
                None,
                len(BODY),
            )
        )
        for name, target in LINKS:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            inner.addfile(info)
            members.append(
                ViewMember(name, MemberRole.SITE_ROOT, MemberKind.SYMLINK, target, 0)
            )
    payload = plain.getvalue()
    path = tmp_path / "links.tar.gz"
    with tarfile.open(path, mode="w") as outer:
        for name, data in ((MANIFEST_MEMBER, b"{}"), (PAYLOAD_MEMBER, payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            outer.addfile(info, io.BytesIO(data))
    view = ArchiveView(
        format_version=1,
        members=tuple(members),
        dump_present=False,
        config_path=None,
        integrity=Integrity.VERIFIED,
        encrypted=False,
        payload=ManifestPayload(
            name=PAYLOAD_MEMBER,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        backup_scope="files",
    )
    return StorageTarget(scheme="file", path=PurePosixPath(path)), view


def test_the_planned_skipped_links_are_the_extracted_skipped_links(
    tmp_path: Path, destination: Path
) -> None:
    from l3io.wp.backup.core.restore import planned_skipped_links

    source, view = _with_links(tmp_path)
    planned = planned_skipped_links(view)
    result = extract_archive(source, destination, view, roles=SITE)

    def key(links: object) -> set[tuple[str, str, str]]:
        return {(s.path, s.link_target, s.reason) for s in links}  # type: ignore[attr-defined]

    assert key(planned) == key(result.skipped_links)
    assert {s.path for s in planned} == {"abs", "up", "sub/up2", "exactly"}
    assert (destination / "inside").is_symlink()


def test_a_repeated_member_name_is_sized_by_its_largest_copy(tmp_path: Path) -> None:
    """A small first copy must not hide a huge second one from the space check."""
    path = tmp_path / "dup.tar"
    with tarfile.open(path, "w") as tar:
        for body in (b"x", b"y" * 5000):
            info = tarfile.TarInfo("wp-root/dup.bin")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    probe = tar_module.TarArchiveProbe()
    digests = probe.member_digests(
        StorageTarget(scheme="file", path=PurePosixPath(path))
    )
    assert digests["wp-root/dup.bin"].size == 5000


# --------------------------------------------------------------------------
# UX-7 (BL-E003-059): extraction refusals in the operator's terms
# --------------------------------------------------------------------------


def test_a_member_refusal_says_the_archive_may_be_bad_and_how_to_check(
    tmp_path: Path, destination: Path
) -> None:
    short = {"site-root/a.php": len(BODY), "site-root/b.php": len(BODY) - 1}
    source, view = _crafted(tmp_path, short)
    with pytest.raises(ExtractionRefusedError) as caught:
        extract_archive(source, destination, view, roles=SITE)
    text = str(caught.value)
    assert "damaged or tampered with" in text
    assert "wpbackup2 verify --archive" in text
    assert "index" not in text


def test_an_interpreter_refusal_does_not_blame_the_archive() -> None:
    text = str(ExtractionRefusedError(None, "this Python's tarfile has no data filter"))
    assert "tampered" not in text
    assert "verify" not in text


def _refusal_texts(source: str) -> list[str]:
    """The literal text of every ``ExtractionRefusedError(...)`` reason in ``source``.

    Reasons built first (``reason = f"..."``) are included: every string
    constant in the module that is assigned to ``reason`` or passed as the
    second argument.
    """
    texts: list[str] = []
    for node in ast.walk(ast.parse(source)):
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ExtractionRefusedError"
            and len(node.args) == 2
        ):
            value = node.args[1]
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "reason" for t in node.targets
        ):
            value = node.value
        if value is not None:
            texts += [
                part.value
                for part in ast.walk(value)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
    return texts


def test_no_extraction_refusal_uses_manifest_vocabulary() -> None:
    texts = _refusal_texts(Path(tar_module.__file__).read_text(encoding="utf-8"))
    assert len(texts) > 10
    assert [t for t in texts if "index" in t] == []


def test_a_planted_index_word_is_caught() -> None:
    planted = (
        'raise ExtractionRefusedError(name, "not in the member index")\n'
        'reason = f"exceeds the indexed {n}"\n'
        'raise ExtractionRefusedError(name, "a hard link")\n'
    )
    texts = _refusal_texts(planted)
    assert sorted(t for t in texts if "index" in t) == [
        "exceeds the indexed ",
        "not in the member index",
    ]
