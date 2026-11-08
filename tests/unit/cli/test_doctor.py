"""FR-27 (E008-S01-001) — ``wpbackup2 doctor`` and ``api.doctor``.

The exit follows ``Diagnosis.ready`` (``OK`` or ``NOT_READY``); the output is
JSON with ``--json`` or off a terminal, with every ``Diagnosis`` field (from
``dataclasses.fields``); the probe runs in the Run workspace; an unsupported
platform is reported, not raised; a Site whose database is unreachable is
``NOT_READY``, never ``UNREACHABLE``; and nothing printed holds a credential.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.preflight import BinaryPreflight, Diagnosis
from l3io.wp.backup.cli import doctor as doctor_mod
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.database import Readiness
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import CredentialSentinel
from tests.unit.cli.conftest import FakeDatabase


@pytest.fixture(autouse=True)
def _empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No known tool on ``PATH``: the probes are deterministic."""
    directory = tmp_path / "empty-bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def _doctor(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    code = main(["--log-format", "json", "doctor", *extra])
    return code, capsys.readouterr().out


def _forced(monkeypatch: pytest.MonkeyPatch, *, ready: bool) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = doctor_mod.diagnose

    def spy(**kwargs: Any) -> Diagnosis:  # noqa: ANN401
        calls.append(kwargs)
        return dataclasses.replace(real(**kwargs), ready=ready)

    monkeypatch.setattr(doctor_mod, "diagnose", spy)
    return calls


@pytest.mark.parametrize(
    ("ready", "code"), [(True, ExitCode.OK), (False, ExitCode.NOT_READY)]
)
def test_the_exit_follows_ready(
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: bool,
    code: ExitCode,
) -> None:
    _forced(monkeypatch, ready=ready)
    exit_code, out = _doctor(capsys, "--json")
    assert exit_code == int(code)
    assert json.loads(out)["ready"] is ready


def test_json_off_a_terminal_without_the_flag_and_every_field(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out = _doctor(capsys)
    document = json.loads(out)
    assert set(document) == {f.name for f in dataclasses.fields(Diagnosis)}


def test_text_on_a_terminal(
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    _, out = _doctor(capsys)
    assert out.startswith("ready: ")
    assert "tool age:" in out


def test_diagnose_gets_a_binary_preflight_probing_from_the_filesystem_root(
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Path] = []
    real = doctor_mod.diagnose

    def spy(**kwargs: Any) -> Diagnosis:  # noqa: ANN401
        preflight = kwargs["preflight"]
        assert isinstance(preflight, BinaryPreflight)
        cwd = preflight._cwd  # noqa: SLF001
        seen.append(cwd)
        return real(**kwargs)

    monkeypatch.setattr(doctor_mod, "diagnose", spy)
    _doctor(capsys, "--json")
    (cwd,) = seen
    # BL-E008-002: not the Run workspace, whose ancestor $TMPDIR is shared.
    assert cwd == Path("/")
    assert not cwd.is_relative_to(workdir)


def test_windows_is_reported_not_raised(
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    # shutil.which consults sys.platform too (3.14: _winapi), so the tool
    # lookup is stubbed; only the platform report is under test here.
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    code, out = _doctor(capsys, "--json")
    document = json.loads(out)
    assert document["platform_supported"] is False
    assert document["ready"] is False
    assert code == int(ExitCode.NOT_READY) == 7


def test_an_unreachable_database_is_not_ready_never_unreachable(
    site: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_database.readiness = Readiness(ready=False, blockers=("unreachable",))
    code, out = _doctor(capsys, "--json", "--wp-dir", str(site))
    document = json.loads(out)
    assert document["ready"] is False
    assert document["database_clients"]["blockers"] == ["unreachable"]
    assert document["database_clients"]["site_given"] is True
    assert code == int(ExitCode.NOT_READY)
    assert code != int(ExitCode.UNREACHABLE)
    assert fake_database.checks, "check_dump was not asked"


def test_a_ready_database_is_reported(
    site: Path,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _doctor(capsys, "--json", "--wp-dir", str(site))
    assert json.loads(out)["database_clients"]["ready"] is True


def test_a_missing_site_root_is_a_blocker_named_by_exit_code(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _doctor(capsys, "--json", "--wp-dir", str(tmp_path / "nowhere"))
    clients = json.loads(out)["database_clients"]
    assert clients["ready"] is False
    (blocker,) = clients["blockers"]
    assert blocker in ExitCode.__members__
    assert code == int(ExitCode.NOT_READY)


def test_a_blocker_says_what_stopped_the_check(
    workdir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-E008-020: the error class and message, not only its code's name."""
    missing = tmp_path / "nowhere"
    _, out = _doctor(capsys, "--json", "--wp-dir", str(missing))
    clients = json.loads(out)["database_clients"]
    (detail,) = clients["blocker_details"]
    assert detail.startswith("InvalidSiteDescriptorError: ")
    assert "site root does not exist" in detail
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    _, out = _doctor(capsys, "--wp-dir", str(missing))
    assert "database blocker: InvalidSiteDescriptorError: " in out


def test_doctor_prints_no_credential(
    credential_sentinel: CredentialSentinel,
    fake_database: FakeDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _doctor(capsys, "--json", "--wp-dir", str(credential_sentinel.site))
    err = capsys.readouterr().err
    assert code in (int(ExitCode.OK), int(ExitCode.NOT_READY))
    for secret in credential_sentinel.secrets:
        assert secret not in out
        assert secret not in err


def test_the_library_doctor_returns_the_diagnosis(workdir: Path) -> None:
    from l3io.wp.backup import api

    diagnosis = api.doctor()
    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.database_clients.site_given is False
