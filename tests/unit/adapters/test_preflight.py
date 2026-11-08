"""FR-27/AD-13 — ``adapters/preflight.py``: ``BinaryPreflight``, ``diagnose``.

Injected ``which``/``probe`` doubles need no real binary; the default probe is
exercised against a temporary executable script. ``preflight_failure`` is the
built decision, so the statuses are checked through it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import preflight as module
from l3io.wp.backup.adapters.credential_providers import (
    ADMIN_PASSWORD_VAR,
    ADMIN_USER_VAR,
    WORDPRESS_PASSWORD_VAR,
    WORDPRESS_USER_VAR,
)
from l3io.wp.backup.adapters.preflight import (
    DATABASE_CLIENTS_CHECKED_BY,
    KNOWN_TOOLS,
    PASSPHRASE_BACKEND_VAR,
    BinaryPreflight,
    diagnose,
    unsupported_platform,
)
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.core.backup import DUMP_REQUIREMENT
from l3io.wp.backup.domain.database import Readiness
from l3io.wp.backup.domain.errors import WpCliConfigFoundError
from l3io.wp.backup.domain.preflight import (
    UNKNOWN_VERSION,
    ToolRequirement,
    ToolStatus,
    preflight_failure,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.conftest import CredentialSentinel

ROOT = Path(__file__).resolve().parents[3]
#: The private exception a ``probe`` raises for a failed probe.
ProbeFailed = module._ProbeFailedError  # noqa: SLF001
AGE = ToolRequirement("age", (1, 3), "ENCRYPT (passphrase, CLI backend)")


class Probe:
    """A ``probe`` double: fixed output, or an exception, per call recorded."""

    def __init__(self, output: str | BaseException = "age v1.3.0") -> None:
        self.output = output
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append((list(argv), cwd, dict(env)))
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


class _Which:
    """A ``which`` double: ``/usr/bin/<name>`` unless ``found`` says otherwise."""

    def __init__(self, found: Mapping[str, str | None]) -> None:
        self.found = found
        self.seen: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.seen.append(name)
        return self.found.get(name, f"/usr/bin/{name}")


def _which(found: Mapping[str, str | None]) -> _Which:
    return _Which(found)


def _check(
    tmp_path: Path, requirement: ToolRequirement, probe: Probe, **found: str | None
) -> ToolStatus:
    pre = BinaryPreflight(cwd=tmp_path, which=_which(found), probe=probe)
    (status,) = pre.check([requirement])
    return status


@pytest.mark.parametrize(
    ("output", "version", "passes"),
    [
        ("age v1.3.0", (1, 3, 0), True),
        ("age v1.3.1\n", (1, 3, 1), True),
        ("v2.0.0", (2, 0, 0), True),
        ("age v1.2.0", (1, 2, 0), False),
    ],
    ids=["at-minimum", "above-patch", "above-major", "too-old"],
)
def test_the_version_is_parsed_and_judged(
    tmp_path: Path, output: str, version: tuple[int, ...], passes: bool
) -> None:
    status = _check(tmp_path, AGE, Probe(output))
    assert status == ToolStatus("age", True, version, "/usr/bin/age")
    failure = preflight_failure(AGE, status)
    assert (failure is None) is passes
    if failure is not None:
        assert failure.found == "1.2.0"


def test_an_absent_tool_is_not_probed(tmp_path: Path) -> None:
    probe = Probe()
    status = _check(tmp_path, AGE, probe, age=None)
    assert status == ToolStatus("age", False, None, None)
    assert probe.calls == []
    failure = preflight_failure(AGE, status)
    assert failure is not None
    assert failure.found == "absent"


def test_output_without_a_version_fails_only_a_minimum(tmp_path: Path) -> None:
    status = _check(tmp_path, AGE, Probe("age: unknown build"))
    assert status == ToolStatus("age", True, None, "/usr/bin/age")
    failure = preflight_failure(AGE, status)
    assert failure is not None
    assert failure.found == UNKNOWN_VERSION
    no_minimum = ToolRequirement("age", None, "x")
    assert preflight_failure(no_minimum, status) is None


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (ProbeFailed("exit 2: boom"), "exit 2: boom"),
        (subprocess.TimeoutExpired(["age"], 10), "timed out after 10s"),
    ],
    ids=["non-zero-exit", "timeout"],
)
def test_a_failed_probe_is_an_error_whatever_the_minimum(
    tmp_path: Path, raised: BaseException, error: str
) -> None:
    status = _check(tmp_path, AGE, Probe(raised))
    assert status == ToolStatus("age", True, None, "/usr/bin/age", error=error)
    for requirement in (AGE, ToolRequirement("age", None, "x")):
        failure = preflight_failure(requirement, status)
        assert failure is not None
        assert failure.found == f"unusable ({error})"


def test_a_long_probe_error_is_truncated(tmp_path: Path) -> None:
    status = _check(tmp_path, AGE, Probe(ProbeFailed("x" * 500)))
    assert status.error is not None
    assert len(status.error) == 200


def test_every_requirement_is_checked_in_order_without_short_circuit(
    tmp_path: Path,
) -> None:
    which = _which({"age": None})
    probe = Probe("1.0.0")
    requirements = [
        AGE,
        ToolRequirement("wp", (2, 9), "relocation"),
        ToolRequirement("age-plugin-batchpass", None, "ENCRYPT"),
    ]
    statuses = BinaryPreflight(cwd=tmp_path, which=which, probe=probe).check(
        requirements
    )
    assert [s.name for s in statuses] == ["age", "wp", "age-plugin-batchpass"]
    assert which.seen == ["age", "wp", "age-plugin-batchpass"]
    assert [s.present for s in statuses] == [False, True, True]


def test_a_tool_with_no_version_argv_is_located_not_probed(tmp_path: Path) -> None:
    probe = Probe()
    requirement = ToolRequirement("age-plugin-batchpass", None, "ENCRYPT")
    status = _check(tmp_path, requirement, probe)
    assert status == ToolStatus(
        "age-plugin-batchpass", True, None, "/usr/bin/age-plugin-batchpass"
    )
    assert probe.calls == []


def test_every_present_tool_is_probed_even_without_a_minimum(tmp_path: Path) -> None:
    probe = Probe("age v1.3.1")
    status = _check(tmp_path, ToolRequirement("age", None, "x"), probe)
    assert status.version == (1, 3, 1)
    assert probe.calls == [(["/usr/bin/age", "--version"], tmp_path, {})]


@pytest.mark.parametrize("root", [False, True], ids=["user", "root"])
def test_wp_is_probed_with_its_data_argv_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: bool
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0 if root else 1000)
    probe = Probe("WP-CLI 2.11.0")
    status = _check(tmp_path, ToolRequirement("wp", (2, 9), "relocation"), probe)
    assert status.version == (2, 11, 0)
    argv, cwd, env = probe.calls[0]
    assert argv == [
        "/usr/bin/wp",
        "cli",
        "version",
        "--skip-plugins",
        "--skip-themes",
        "--skip-packages",
        "--no-color",
        *(["--allow-root"] if root else []),
    ]
    assert "--path" not in " ".join(argv)
    assert cwd == tmp_path
    assert env == {
        "WP_CLI_CONFIG_PATH": os.devnull,
        "WP_CLI_PACKAGES_DIR": os.path.join(os.devnull, "wp-cli-packages"),  # noqa: PTH118
    }


@pytest.mark.parametrize("name", ["mysqldump", DUMP_REQUIREMENT.name, "zz-unknown"])
def test_an_unknown_name_raises_before_anything_is_located(
    tmp_path: Path, name: str
) -> None:
    which = _which({})
    probe = Probe()
    pre = BinaryPreflight(cwd=tmp_path, which=which, probe=probe)
    with pytest.raises(ValueError, match=name):
        pre.check([AGE, ToolRequirement(name, None, "x")])
    assert which.seen == []
    assert probe.calls == []


def test_no_requirements_give_no_statuses(tmp_path: Path) -> None:
    assert (
        BinaryPreflight(cwd=tmp_path, which=_which({}), probe=Probe()).check([]) == ()
    )


def test_each_requirement_logs_preflight_checked(tmp_path: Path) -> None:
    pre = BinaryPreflight(
        cwd=tmp_path, which=_which({"wp": None}), probe=Probe("age v1.2.0")
    )
    with capture_logs() as logs:
        pre.check([AGE, ToolRequirement("wp", (2, 9), "relocation")])
    records = [r for r in logs if r["event"] == "preflight_checked"]
    assert [(r["tool"], r["outcome"], r["log_level"]) for r in records] == [
        ("age", "too_old", "error"),
        ("wp", "missing", "error"),
    ]
    assert records[0]["version"] == "1.2.0"
    assert records[0]["minimum"] == "1.3"
    assert records[0]["needed_for"] == AGE.needed_for
    assert records[0]["path"] == "/usr/bin/age"
    with capture_logs() as logs:
        BinaryPreflight(cwd=tmp_path, which=_which({}), probe=Probe()).check([AGE])
    assert [(r["outcome"], r["log_level"]) for r in logs] == [("ok", "debug")]


# --- the real default probe -------------------------------------------------


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_default_probe_runs_a_real_executable(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    age = _script(bin_dir, "age", 'echo "v1.3.1"')
    pre = BinaryPreflight(cwd=tmp_path, which=lambda name: str(age))
    (status,) = pre.check([AGE])
    assert status == ToolStatus("age", True, (1, 3, 1), str(age))


def test_the_default_probe_reports_a_non_zero_exit(tmp_path: Path) -> None:
    age = _script(tmp_path, "age", 'echo "no such flag" >&2; exit 3')
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.error == "exit 3: no such flag"


def test_the_default_probe_survives_output_that_is_not_utf8(tmp_path: Path) -> None:
    age = _script(tmp_path, "age", "printf 'age v1.3.0 \\377\\376\\n'")
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.version == (1, 3, 0)
    assert status.error is None


def test_the_default_probe_reports_an_os_error(tmp_path: Path) -> None:
    age = tmp_path / "age"
    age.write_text("not executable", encoding="utf-8")
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.error is not None
    assert status.error.startswith("PermissionError")


def test_the_probe_environment_is_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "AGE_PASSPHRASE_FD",
        "L3IO_WP_BACKUP_PASSPHRASE",
        WORDPRESS_PASSWORD_VAR,
        "MYSQL_PWD",
    ):
        monkeypatch.setenv(name, "planted")
    seen: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        seen.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    age = _script(tmp_path, "age", 'echo "v1.3.1"')
    wp = _script(tmp_path, "wp", 'echo "WP-CLI 2.11.0"')
    paths = {"age": str(age), "wp": str(wp)}
    BinaryPreflight(cwd=tmp_path, which=paths.get).check(
        [AGE, ToolRequirement("wp", None, "relocation")]
    )
    assert [set(kw["env"]) for kw in seen] == [
        {"PATH", "HOME", "LC_ALL"},
        {"PATH", "HOME", "LC_ALL", "WP_CLI_CONFIG_PATH", "WP_CLI_PACKAGES_DIR"},
    ]
    assert seen[0]["env"]["LC_ALL"] == "C"
    assert all(kw.get("shell", False) is False for kw in seen)
    assert all(kw["start_new_session"] and kw["cwd"] == tmp_path for kw in seen)


def test_the_known_tools_are_exactly_the_binaries_this_package_runs() -> None:
    assert set(KNOWN_TOOLS) == {"age", "age-plugin-batchpass", "wp"}
    assert DUMP_REQUIREMENT.name not in KNOWN_TOOLS


# --- unsupported_platform ---------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "refused"),
    [("linux", False), ("darwin", False), ("win32", True), ("cygwin", True)],
)
def test_unsupported_platform(platform: str, refused: bool) -> None:
    reason = unsupported_platform(platform)
    assert (reason is not None) is refused
    if reason is not None:
        assert platform in reason


# --- diagnose ----------------------------------------------------------------


def _absent_preflight(tmp_path: Path) -> BinaryPreflight:
    return BinaryPreflight(cwd=tmp_path, which=lambda name: None, probe=Probe())


def test_diagnose_lists_every_known_tool_and_age_as_absent(tmp_path: Path) -> None:
    diagnosis = diagnose(preflight=_absent_preflight(tmp_path), environ={})
    assert [t.name for t in diagnosis.tools] == list(KNOWN_TOOLS)
    assert {t.name: t.present for t in diagnosis.tools}["age"] is False
    assert diagnosis.python
    assert diagnosis.platform == sys.platform
    assert diagnosis.platform_supported is (unsupported_platform() is None)
    assert diagnosis.passphrase_backend == "auto"
    assert diagnosis.database_clients.checked_by == DATABASE_CLIENTS_CHECKED_BY
    assert diagnosis.database_clients.site_given is False
    assert diagnosis.storage_target is None
    assert diagnosis.ready is True
    json.dumps(diagnosis.to_dict())


def test_diagnose_reports_versions_through_the_probe(tmp_path: Path) -> None:
    pre = BinaryPreflight(cwd=tmp_path, which=_which({}), probe=Probe("v1.3.1"))
    tools = {t.name: t for t in diagnose(preflight=pre, environ={}).tools}
    assert tools["age"].version == (1, 3, 1)
    assert tools["wp"].version == (1, 3, 1)


def _optional_dependencies() -> set[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return set(tomllib.load(handle)["project"]["optional-dependencies"])


def test_diagnose_extras_are_the_pyproject_extras(tmp_path: Path) -> None:
    """Scope from ``pyproject.toml``, not from the metadata ``diagnose`` reads."""
    extras = diagnose(preflight=_absent_preflight(tmp_path), environ={}).extras
    expected = _optional_dependencies()
    assert expected, "pyproject.toml declares no extras: nothing was checked"
    assert {e.name for e in extras} == expected
    installed = {e.name: e for e in extras}
    assert installed["database"].installed, installed["database"].missing


def test_diagnose_reports_a_missing_extra_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = module.importlib.metadata.version

    def version(name: str) -> str:
        if name == "opentelemetry-sdk":
            raise module.importlib.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(module.importlib.metadata, "version", version)
    extras = diagnose(preflight=_absent_preflight(tmp_path), environ={}).extras
    otel = {e.name: e for e in extras}["otel"]
    assert otel.installed is False
    assert otel.missing == ("opentelemetry-sdk<2,>=1.40",)


def test_diagnose_reports_unavailable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> NoReturn:
        raise module.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.importlib.metadata, "metadata", missing)
    (extra,) = diagnose(preflight=_absent_preflight(tmp_path), environ={}).extras
    assert extra.installed is False
    assert extra.missing == ("<metadata unavailable>",)


def test_diagnose_checks_an_extra_another_distribution_provides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aws`` is ``l3io-wordpress-database[aws]``: database's own ``aws`` extra."""
    nested = module._gated("l3io-wordpress-database", "aws")  # noqa: SLF001
    assert nested, "database's aws extra names nothing: nothing was checked"
    absent = nested[0].name
    real = module.importlib.metadata.version

    def version(name: str) -> str:
        if name == absent:
            raise module.importlib.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(module.importlib.metadata, "version", version)
    extras = {
        e.name: e
        for e in diagnose(preflight=_absent_preflight(tmp_path), environ={}).extras
    }
    assert extras["aws"].installed is False
    assert any(m.startswith(absent) for m in extras["aws"].missing)
    assert extras["database"].installed is True


def test_a_core_requirement_with_an_environment_marker_is_no_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module.importlib.metadata,
        "requires",
        lambda name: [
            'tomli>=2; python_version >= "3"',
            'boto3>=1; extra == "aws"',
        ],
    )
    gated = module._gated("anything", "aws")  # noqa: SLF001
    assert [r.name for r in gated] == ["boto3"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a mode-000 directory")
def test_diagnose_reports_an_untraversable_target(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        target = StorageTarget(
            scheme="file", path=PurePosixPath(locked / "sub" / "a.tar.gz")
        )
        diagnosis = diagnose(
            preflight=_absent_preflight(tmp_path), environ={}, target=target
        )
    finally:
        locked.chmod(0o700)
    assert diagnosis.storage_target is not None
    assert (diagnosis.storage_target.exists, diagnosis.storage_target.writable) == (
        False,
        False,
    )
    assert diagnosis.ready is False


def test_diagnose_reports_the_storage_target(tmp_path: Path) -> None:
    pre = _absent_preflight(tmp_path)
    good = StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tar.gz"))
    status = diagnose(preflight=pre, environ={}, target=good).storage_target
    assert status is not None
    assert (status.exists, status.writable) == (False, True)
    gone = StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "no" / "a.tgz"))
    diagnosis = diagnose(preflight=pre, environ={}, target=gone)
    assert diagnosis.storage_target is not None
    assert diagnosis.storage_target.writable is False
    assert diagnosis.ready is False


def test_diagnose_carries_database_findings_and_runs_nothing(tmp_path: Path) -> None:
    probe = Probe()
    pre = BinaryPreflight(cwd=tmp_path, which=lambda n: None, probe=probe)
    readiness = Readiness(ready=False, blockers=("CLIENT_TOOL_MISSING",))
    diagnosis = diagnose(preflight=pre, environ={}, database=readiness)
    assert diagnosis.database_clients.site_given is True
    assert diagnosis.database_clients.blockers == ("CLIENT_TOOL_MISSING",)
    assert diagnosis.ready is False
    assert probe.calls == []


@pytest.mark.parametrize(
    ("value", "reported"),
    [("cli", "cli"), ("binder", "binder"), ("auto", "auto"), ("s3cr3t", "invalid")],
)
def test_diagnose_reports_the_backend_never_a_raw_value(
    tmp_path: Path, value: str, reported: str
) -> None:
    diagnosis = diagnose(
        preflight=_absent_preflight(tmp_path), environ={PASSPHRASE_BACKEND_VAR: value}
    )
    assert diagnosis.passphrase_backend == reported


@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
def test_the_diagnosis_is_sentinel_free(
    tmp_path: Path, credential_sentinel: CredentialSentinel
) -> None:
    secret = credential_sentinel.password
    for name in (
        WORDPRESS_USER_VAR,
        WORDPRESS_PASSWORD_VAR,
        ADMIN_USER_VAR,
        ADMIN_PASSWORD_VAR,
        "MYSQL_PWD",
        PASSPHRASE_BACKEND_VAR,
    ):
        credential_sentinel.monkeypatch.setenv(name, secret)
    diagnosis = diagnose(
        preflight=_absent_preflight(tmp_path),
        environ=os.environ,
        target=StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tgz")),
    )
    rendered = repr(diagnosis) + json.dumps(diagnosis.to_dict())
    assert secret not in rendered
    assert credential_sentinel.passphrase not in rendered


def test_the_default_lookup_ignores_relative_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from l3io.wp.backup.adapters.preflight import _locate

    planted = tmp_path / "planted"
    planted.mkdir()
    name = f"tool-{uuid.uuid4().hex}"
    exe = planted / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", f"{os.pathsep}.{os.pathsep}/nonexistent-{name}")
    assert _locate(name) is None
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}.")
    assert _locate(name) == str(exe)


# --------------------------------------------------------------------------
# E008-S01-001: the probe is reachable from every root, so it is hardened
# --------------------------------------------------------------------------

WP = ToolRequirement("wp", (2, 12), "relocation")


@pytest.mark.parametrize("name", module.WP_CLI_CONFIG_FILES)
@pytest.mark.parametrize("above", [True, False], ids=["above", "in-cwd"])
def test_a_wp_cli_config_above_the_workspace_refuses_before_wp_runs(
    tmp_path: Path, name: str, above: bool
) -> None:
    """BL-E007-007: WP-CLI would read it; the probe is never started."""
    workspace = tmp_path / "state" / "run"
    workspace.mkdir(parents=True)
    planted = (tmp_path if above else workspace) / name
    planted.write_text("require: [evil.php]\n", encoding="utf-8")
    probe = Probe("WP-CLI 2.12.0")
    with pytest.raises(WpCliConfigFoundError) as caught:
        _check(workspace, WP, probe)
    assert caught.value.path == planted
    assert int(exit_code_for(caught.value)) == 3
    assert probe.calls == []


def test_a_wp_cli_config_behind_a_symlinked_workspace_is_found(tmp_path: Path) -> None:
    real = tmp_path / "real" / "run"
    real.mkdir(parents=True)
    (tmp_path / "real" / "wp-cli.yml").write_text("x: 1\n", encoding="utf-8")
    link = tmp_path / "elsewhere"
    link.mkdir()
    (link / "run").symlink_to(real)
    probe = Probe("WP-CLI 2.12.0")
    with pytest.raises(WpCliConfigFoundError):
        _check(link / "run", WP, probe)
    assert probe.calls == []


def test_age_is_probed_whatever_wp_cli_config_is_above(tmp_path: Path) -> None:
    (tmp_path / "wp-cli.yml").write_text("x: 1\n", encoding="utf-8")
    status = _check(tmp_path, AGE, Probe("age v1.3.0"))
    assert status.version == (1, 3, 0)


def test_the_version_comes_from_the_first_stdout_line_only(tmp_path: Path) -> None:
    age = _script(tmp_path, "age", 'echo "age (dev)"; echo "v9.9.9"; echo v9.9 >&2')
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.version is None


def test_control_characters_never_reach_the_error_or_the_log(tmp_path: Path) -> None:
    age = _script(tmp_path, "age", "printf 'bad\\033[31m\\a\\rred\\n' >&2; exit 2")
    with capture_logs() as logs:
        (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.error is not None
    assert not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in status.error)
    for record in logs:
        assert "\x1b" not in str(record.get("error", ""))


def test_output_past_the_limit_kills_the_probe(tmp_path: Path) -> None:
    age = _script(tmp_path, "age", "yes v1.3.0")
    started = time.monotonic()
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.error is not None
    assert str(module.PROBE_OUTPUT_LIMIT) in status.error
    assert time.monotonic() - started < 9


def test_a_timeout_kills_the_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redteam M-3: a grandchild holding the pipe is killed with its parent."""
    marker = tmp_path / "survived"
    age = _script(tmp_path, "age", f"(sleep 3; touch {marker}) & sleep 30")
    monkeypatch.setattr(module, "_PROBE_TIMEOUT_S", 1)
    started = time.monotonic()
    (status,) = BinaryPreflight(cwd=tmp_path, which=lambda n: str(age)).check([AGE])
    assert status.error == "timed out after 1s"
    assert time.monotonic() - started < 5
    time.sleep(3)
    assert not marker.exists(), "a grandchild outlived the probe"


def test_run_bounded_returns_both_streams(tmp_path: Path) -> None:
    script = _script(tmp_path, "t", "echo out; echo err >&2; exit 4")
    result = module.run_bounded(
        [str(script)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=5, limit=1024
    )
    assert (result.returncode, result.stdout, result.stderr) == (4, "out\n", "err\n")


def test_the_group_is_killed_while_the_leader_is_still_unreaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E008-018: a zombie leader keeps its pid, so the pgid cannot be reused."""
    script = _script(tmp_path, "t", "exit 0")
    unreaped: list[bool] = []
    real = module._kill_group  # noqa: SLF001

    def spy(leader: int) -> None:
        try:
            info = os.waitid(os.P_PID, leader, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            info = None  # already reaped: its pid could be anyone's now
        unreaped.append(info is not None)
        real(leader)

    monkeypatch.setattr(module, "_kill_group", spy)
    result = module.run_bounded(
        [str(script)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=5, limit=1024
    )
    assert result.returncode == 0
    assert unreaped == [True]


def test_run_bounded_raises_past_the_limit_on_stderr(tmp_path: Path) -> None:
    script = _script(tmp_path, "t", "head -c 5000 /dev/zero >&2")
    with pytest.raises(module.ChildOutputLimitError) as caught:
        module.run_bounded(
            [str(script)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=5, limit=1000
        )
    assert caught.value.limit == 1000


def test_diagnose_logs_an_absent_tool_below_error(tmp_path: Path) -> None:
    """BL-E005-003: nothing required it, so its absence is not an error."""
    pre = BinaryPreflight(
        cwd=tmp_path, which=_which({"age": None}), probe=Probe(), optional=True
    )
    with capture_logs() as logs:
        diagnose(preflight=pre, environ={})
    age = [r for r in logs if r["event"] == "preflight_checked" and r["tool"] == "age"]
    assert [r["log_level"] for r in age] == ["info"]
    required = BinaryPreflight(cwd=tmp_path, which=_which({"age": None}), probe=Probe())
    with capture_logs() as logs:
        required.check([AGE])
    assert [r["log_level"] for r in logs if r["event"] == "preflight_checked"] == [
        "error"
    ]


def test_unsupported_platform_reads_the_platform_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert unsupported_platform() is not None


def test_an_interrupt_kills_the_child_group_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review H-1: SIGTERM/Ctrl-C during the wait kill the child, not wait it out."""
    marker = tmp_path / "survived"
    script = _script(tmp_path, "t", f"sleep 3; touch {marker}")

    def interrupted(pid: int, argv: object, timeout: float) -> bool:
        # The wait for the leader (BL-E008-018: ``_wait_unreaped``) is where
        # SIGTERM's RunTerminated or Ctrl-C lands.
        del pid, argv, timeout
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_wait_unreaped", interrupted)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        module.run_bounded(
            [str(script)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=30, limit=64
        )
    assert time.monotonic() - started < 2.5
    time.sleep(3.5)
    assert not marker.exists(), "the child outlived the interrupted call"


def test_a_grandchild_outside_the_group_does_not_unbound_the_timeout(
    tmp_path: Path,
) -> None:
    """Review M-2: a setsid grandchild holding the pipes is abandoned, bounded."""
    script = _script(tmp_path, "t", "setsid sleep 6 & sleep 30")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        module.run_bounded(
            [str(script)], cwd=tmp_path, env={"PATH": os.defpath}, timeout=1, limit=64
        )
    assert time.monotonic() - started < 4.5


def test_doctor_reports_a_wp_cli_config_instead_of_raising(tmp_path: Path) -> None:
    """Review M-3: diagnose with a wp-cli.yml above the workspace still reports."""
    workspace = tmp_path / "run"
    workspace.mkdir()
    (tmp_path / "wp-cli.yml").write_text("x: 1\n", encoding="utf-8")
    probe = Probe("WP-CLI 2.12.0")
    pre = BinaryPreflight(cwd=workspace, which=_which({}), probe=probe, optional=True)
    diagnosis = diagnose(preflight=pre, environ={})
    (wp,) = [t for t in diagnosis.tools if t.name == "wp"]
    assert wp.error is not None
    assert "wp-cli.yml" in wp.error
    assert not diagnosis.ready
    assert all(call[0][0] != "/usr/bin/wp" for call in probe.calls)
