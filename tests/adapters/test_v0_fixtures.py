"""FR-10, AD-23 — the committed v0 fixture Archives read as the story says.

Scope comes from the directories, never from the table: every Archive directly
in ``tests/fixtures/archives/v0/`` (the passing set) and in ``v0/malformed/``
(the failing set), each selected through ``tests/archive_fixtures.py::
archive_files`` and asserted non-empty. :data:`EXPECTED` says what each must do,
keyed by file name; a fixture on disk with no entry, or an entry with no
fixture, fails. Each is driven through the real ``TarArchiveProbe`` →
``classify`` → ``core.verify`` path.

The CLI half (exit codes and output) is ``tests/integration/test_verify_cli.py``,
and the extraction half of the ``escape-*`` and ``symlink-escape`` fixtures is
``tests/contracts/test_extraction.py`` (CI's AD-11 containment step).
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from l3io.wp.backup.adapters.tar import TarArchiveProbe
from l3io.wp.backup.cli import main
from l3io.wp.backup.core.verify import verify, verify_probe
from l3io.wp.backup.domain.archive_format import (
    V0_DUMP_MEMBER,
    V0_SITE_ROOT,
    ArchiveView,
    Integrity,
    MemberRole,
    VerificationReason,
    classify,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.archive_fixtures import SIDECAR_SUFFIXES, archive_files

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V0 = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v0"
MALFORMED = V0 / "malformed"
RELEASED = V0 / "released-0.2.21-all.tar.gz"
WP_CONFIG = PROJECT_ROOT / "tests" / "data" / "wp-config.php"
PROBE = TarArchiveProbe()
R = VerificationReason
CONFIG = f"{V0_SITE_ROOT}/wp-config.php"


@dataclass(frozen=True)
class Passes:
    dump: bool
    config: bool = True


@dataclass(frozen=True)
class Fails:
    reason: VerificationReason
    member: str | None


EXPECTED: dict[str, Passes | Fails] = {
    "released-0.2.21-all.tar.gz": Passes(dump=True),
    "synthetic-all-pax.tar.gz": Passes(dump=True),
    "synthetic-all-gnu.tar.gz": Passes(dump=True),
    "synthetic-all-symlink.tar.gz": Passes(dump=True),
    "synthetic-all-longname.tar.gz": Passes(dump=True),
    "synthetic-files-only.tar.gz": Passes(dump=False),
    "synthetic-renamed.bak": Passes(dump=True),
    "symlink-escape.tar.gz": Passes(dump=False),
    "missing-wp-root.tar.gz": Fails(R.UNRECOGNISED_FORMAT, V0_DUMP_MEMBER),
    "stray-manifest-late.tar.gz": Fails(R.UNRECOGNISED_FORMAT, V0_DUMP_MEMBER),
    "escape-dotdot.tar.gz": Fails(
        R.UNSAFE_MEMBER_PATH, f"{V0_SITE_ROOT}/../escape\n\x1b[31mred"
    ),
    "escape-absolute.tar.gz": Fails(R.UNSAFE_MEMBER_PATH, f"{V0_SITE_ROOT}//etc/x"),
    "hardlink.tar.gz": Fails(R.UNSUPPORTED_MEMBER_TYPE, f"{V0_SITE_ROOT}/x"),
    "truncated.tar.gz": Fails(R.TRUNCATED, None),
}

#: The operator-secret prefixes ``credential_sentinel`` plants
#: (``tests/conftest.py``, spelled inline there): the passwords and the
#: passphrase. A released fixture written outside the suite can hold one only
#: if it was made from a test Run's leftovers; the config check below is the
#: one that guards against a real site.
SENTINEL_PREFIXES = ("sentinel-pw-", "sentinel-pw2-", "sentinel-pw3-", "sentinel-pp-")

PASSING = archive_files(V0)
FAILING = archive_files(MALFORMED)


def _target(path: Path) -> StorageTarget:
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def _verified(path: Path) -> tuple[ArchiveView, Any]:
    target = _target(path)
    index = PROBE.member_index(target)
    view = classify(index)
    return view, verify(view, PROBE.member_digests(target), index)


def test_the_selection_rule(tmp_path: Path) -> None:
    assert SIDECAR_SUFFIXES  # the rule; the plants below are its reach
    # The real sidecar names, spelled here rather than built from the rule.
    for name in (
        "released-0.2.21-all.provenance.json",
        "encrypted-passphrase.passphrase.txt",
    ):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "make.py").write_text("", encoding="utf-8")
    (tmp_path / "malformed").mkdir()
    (tmp_path / "x.tar.gz").write_bytes(b"")
    (tmp_path / "x.bak").write_bytes(b"")
    assert archive_files(tmp_path) == [tmp_path / "x.bak", tmp_path / "x.tar.gz"]


def test_both_sets_are_non_empty() -> None:
    assert PASSING, f"no v0 fixture Archives in {V0}"
    assert FAILING, f"no malformed v0 fixture Archives in {MALFORMED}"


def test_no_archive_hides_in_another_directory() -> None:
    """Every walker reads ``v0/`` and ``v0/malformed/``; nothing else may exist."""
    subdirectories = {
        p.name for p in V0.iterdir() if p.is_dir() and p.name != "__pycache__"
    }
    assert subdirectories == {MALFORMED.name}


def test_the_table_and_the_directories_agree() -> None:
    on_disk = [p.name for p in (*PASSING, *FAILING)]
    assert len(on_disk) == len(set(on_disk)), "a name in both sets"
    assert sorted(on_disk) == sorted(EXPECTED), (
        f"no entry for {sorted(set(on_disk) - set(EXPECTED))}; "
        f"no fixture for {sorted(set(EXPECTED) - set(on_disk))}"
    )


@pytest.mark.parametrize("path", PASSING, ids=lambda p: p.name)
def test_a_passing_fixture_is_structural_only(path: Path) -> None:
    expected = EXPECTED[path.name]
    assert isinstance(expected, Passes), f"{path.name} is in the passing set"
    view, result = _verified(path)
    assert (result.passed, result.integrity, result.format_version) == (
        True,
        Integrity.STRUCTURAL_ONLY,
        None,
    )
    assert result.failures == ()
    assert view.payload is None  # no Manifest: nothing claims a checksum
    assert view.dump_present is expected.dump
    assert view.config_path == (CONFIG if expected.config else None)
    for member in view.members:
        role = (
            MemberRole.DUMP if member.path == V0_DUMP_MEMBER else MemberRole.SITE_ROOT
        )
        assert member.role is role, member.path
        assert member.link_target is None, member.path
        assert member.size is None, member.path
    paths = {m.path for m in view.members}
    assert (V0_DUMP_MEMBER in paths) is expected.dump
    assert V0_SITE_ROOT in paths  # the directory member, as tarfile spells it


@pytest.mark.parametrize("path", FAILING, ids=lambda p: p.name)
def test_a_malformed_fixture_fails_naming_its_reason(path: Path) -> None:
    expected = EXPECTED[path.name]
    assert isinstance(expected, Fails), f"{path.name} is in the failing set"
    target = _target(path)
    index = PROBE.member_index(target)
    _, result = verify_probe(index, PROBE.member_digests(target))
    assert not result.passed
    assert [(f.member, f.reason) for f in result.failures][:1] == [
        (expected.member, expected.reason)
    ]
    assert result.integrity is not Integrity.VERIFIED


def test_the_renamed_fixture_classifies_as_its_twin() -> None:
    twin, _ = _verified(V0 / "synthetic-all-pax.tar.gz")
    renamed, _ = _verified(V0 / "synthetic-renamed.bak")
    assert renamed == twin


def _verify_lines(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, list[dict[str, Any]]]:
    code = main(["--log-format", "json", "verify", "--archive", str(path)])
    captured = capsys.readouterr()
    return (
        code,
        captured.out + captured.err,
        [
            json.loads(line)
            for line in captured.err.splitlines()
            if line.startswith("{")
        ],
    )


@pytest.mark.parametrize("path", PASSING, ids=lambda p: p.name)
def test_a_verify_run_logs_one_v0_record(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, text, lines = _verify_lines(path, capsys)
    assert code == 0
    records = [r for r in lines if r["event"] == "phase_completed"]
    assert [r["phase"] for r in records] == ["VERIFY"]
    (record,) = records
    run_id = next(r["l3io_run_id"] for r in lines if r["event"] == "run_started")
    assert (
        record["format_version"],
        record["integrity"],
        record["passed"],
        record["l3io_run_id"],
    ) == ("v0", "STRUCTURAL_ONLY", True, run_id)
    assert "VERIFIED" not in text


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_a_hostile_member_name_is_rendered_escaped(
    log_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = MALFORMED / "escape-dotdot.tar.gz"
    hostile = EXPECTED[path.name]
    assert isinstance(hostile, Fails)
    assert hostile.member is not None
    assert "\n" in hostile.member
    assert "\x1b" in hostile.member
    code = main(["--log-format", log_format, "verify", "--archive", str(path)])
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert code == 8
    assert "UNSAFE_MEMBER_PATH" in rendered
    assert "\x1b" not in rendered
    assert "escape\n" not in rendered


def test_the_released_fixture_carries_no_operator_secret() -> None:
    assert RELEASED.is_file(), (
        f"{RELEASED.name} is the fidelity anchor and must be committed "
        "(written once by the released wpbackup2 0.2.21; see its provenance file)"
    )
    with tarfile.open(RELEASED) as archive:
        member = archive.extractfile(V0_DUMP_MEMBER)
        config = archive.extractfile(CONFIG)
        assert member is not None
        assert config is not None
        dump = member.read().decode("utf-8", errors="replace")
        embedded = config.read()
    assert dump.strip(), "the released fixture's Dump is empty"
    assert not [p for p in SENTINEL_PREFIXES if p in dump]

    # Only the public test credentials: the Site Root's config is the test
    # one, but for DB_PASSWORD. The released fixture is never regenerated (its
    # SHA-256 is pinned in the provenance file), so it keeps the test config's
    # password from before BL-E004-064 replaced it with a unique sentinel;
    # every other line must still match.
    def without_password(text: bytes) -> list[bytes]:
        return [line for line in text.splitlines() if b"'DB_PASSWORD'" not in line]

    assert without_password(embedded) == without_password(WP_CONFIG.read_bytes())
    assert embedded != WP_CONFIG.read_bytes(), (
        "the released fixture now carries the sentinel: drop this exception"
    )
