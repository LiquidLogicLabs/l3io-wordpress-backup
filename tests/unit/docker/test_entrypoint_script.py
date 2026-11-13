"""Owner decision 87 — the image runs as the user it is given and never re-owns.

``l3io-wpbackup-entrypoint``, ``l3io-wpbackup-scheduler`` and the two s6
scripts are run as written (copied, executable, onto ``PATH``) in a stub
environment:

- ``id`` answers ``-u``/``-g`` from ``STUB_UID``/``STUB_GID`` (the test's own
  ids when unset), so a script can be told it started as root;
- ``setpriv`` records the ids it was asked for and runs the command with
  ``STUB_UID``/``STUB_GID`` set to them (no real switch);
- ``wpbackup2`` and ``supercronic`` record who ran them and with what;
  ``supercronic -test`` accepts any crontab.

The folders the scripts check are real temporary folders, so the access
checks are the real ones, made as the test's own user (skipped as root, which
reads and writes whatever the mode bits say). The built image runs the same
scripts for real (``tests/sidecar/run.sh``).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
IMAGE_ROOT = ROOT / "docker" / "root"
BIN = IMAGE_ROOT / "usr" / "local" / "bin"
S6 = IMAGE_ROOT / "etc" / "s6-overlay" / "s6-rc.d"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")
AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads and writes whatever the mode bits say"
)

UID = os.getuid()
GID = os.getgid()
#: What ``id`` says when a script is told it started as root.
AS_ROOT_IDS = {"STUB_UID": "0", "STUB_GID": "0"}

_STUBS = {
    "id": """#!/usr/bin/env bash
case "${1:-}" in
  -u) echo "${STUB_UID:-$(/usr/bin/id -u)}" ;;
  -g) echo "${STUB_GID:-$(/usr/bin/id -g)}" ;;
  *) exec /usr/bin/id "$@" ;;
esac
""",
    "setpriv": """#!/usr/bin/env bash
for a in "$@"; do
  case $a in
    --reuid=*) u=${a#--reuid=} ;;
    --regid=*) g=${a#--regid=} ;;
    --) shift; break ;;
  esac
  shift
done
echo "setpriv $u:$g $(basename "$1") ${*:2}" >>"$STUB_LOG"
STUB_UID=$u STUB_GID=$g exec "$@"
""",
    "wpbackup2": """#!/usr/bin/env bash
echo "wpbackup2 as $(id -u):$(id -g): $*" >>"$STUB_LOG"
""",
    "supercronic": """#!/usr/bin/env bash
[[ $1 == -test ]] && exit 0
echo "supercronic as $(id -u):$(id -g): $*" >>"$STUB_LOG"
""",
    "sleep": """#!/usr/bin/env bash
echo "sleep as $(id -u):$(id -g): $*" >>"$STUB_LOG"
""",
}


class Image:
    """The scripts and folders of one container, in ``tmp_path``."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in _STUBS.items():
            self._install(name, text)
        for script in BIN.iterdir():
            self._install(script.name, script.read_text(encoding="utf-8"))
        for service in ("init-wpbackup-config", "svc-wpbackup-schedule"):
            self._install(service, (S6 / service / "run").read_text(encoding="utf-8"))
        self.log = tmp_path / "log"
        self.log.touch()
        self.data = tmp_path / "data"
        self.state = self.data / "state"
        self.tmp = self.data / "tmp"
        self.backups = self.data / "backups"
        self.html = self.data / "html"
        for folder in (self.state, self.tmp, self.backups, self.html):
            folder.mkdir(parents=True, mode=0o700)
        (self.html / "wp-config.php").write_text("<?php\n", encoding="utf-8")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "STUB_LOG": str(self.log),
            "XDG_STATE_HOME": str(self.state),
            "TMPDIR": str(self.tmp),
            "L3IO_WP_BACKUP_ARCHIVE_DIR": str(self.backups),
            "L3IO_WP_BACKUP_WP_DIR": str(self.html),
            "WPBACKUP_SCHEDULE": "0 3 * * *",
            "WPBACKUP_CRONTAB": "",
        }

    def _install(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def run(
        self, script: str, *args: str, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the repository's own scripts
            [str(BASH), str(self.bin / script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def owners(self) -> list[tuple[str, int, int, int]]:
        """Every path under the data folders with its owner, group and mode."""
        return [
            (str(p), p.lstat().st_uid, p.lstat().st_gid, p.lstat().st_mode)
            for p in sorted([self.data, *self.data.rglob("*")])
        ]

    def schedule_dir(self, uid: int) -> Path:
        return self.tmp / f"wpbackup-schedule-{uid}"


@pytest.fixture
def image(tmp_path: Path) -> Image:
    return Image(tmp_path)


# ---- started as root ------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "ids"),
    [({}, "33:33"), ({"PUID": "1234", "PGID": "2345"}, "1234:2345")],
    ids=["default-33", "puid-pgid"],
)
def test_a_root_one_shot_drops_to_puid_pgid_and_changes_no_owner(
    image: Image, env: dict[str, str], ids: str
) -> None:
    before = image.owners()
    result = image.run("l3io-wpbackup-entrypoint", "backup", **AS_ROOT_IDS, **env)
    assert result.returncode == 0, result.stderr
    assert image.calls() == [
        f"setpriv {ids} l3io-wpbackup-entrypoint backup",
        f"wpbackup2 as {ids}: backup",
    ]
    assert image.owners() == before


def test_a_root_one_shot_command_runs_as_puid_too(image: Image) -> None:
    result = image.run("l3io-wpbackup-entrypoint", "sleep", "1", **AS_ROOT_IDS)
    assert result.returncode == 0, result.stderr
    assert image.calls()[-1] == "sleep as 33:33: 1"


def test_puid_0_keeps_root(image: Image) -> None:
    result = image.run(
        "l3io-wpbackup-entrypoint", "verify", **AS_ROOT_IDS, PUID="0", PGID="0"
    )
    assert result.returncode == 0, result.stderr
    assert image.calls() == ["wpbackup2 as 0:0: verify"]


@pytest.mark.parametrize("env", [{"PUID": "www-data"}, {"PGID": "-1"}])
def test_a_non_numeric_puid_is_refused(image: Image, env: dict[str, str]) -> None:
    result = image.run("l3io-wpbackup-entrypoint", "backup", STUB_UID="0", **env)
    assert result.returncode == 2
    assert "PUID and PGID must be numeric ids" in result.stderr
    assert image.calls() == []


@pytest.mark.skipif(Path("/init").exists(), reason="this host has an /init")
@pytest.mark.parametrize("args", [(), ("schedule",)], ids=["default", "schedule"])
def test_a_root_scheduled_start_hands_over_to_s6(
    image: Image, args: tuple[str, ...]
) -> None:
    """``exec /init``: there is none here, so the attempt is what is seen."""
    result = image.run("l3io-wpbackup-entrypoint", *args, **AS_ROOT_IDS)
    assert result.returncode == 127
    assert "/init" in result.stderr
    assert image.calls() == []


@AS_ROOT
def test_the_s6_path_checks_and_runs_supercronic_as_puid(image: Image) -> None:
    """init-wpbackup-config, then svc-wpbackup-schedule, started as root."""
    before = image.owners()
    ids = {"STUB_UID": "0", "STUB_GID": "0", "PUID": str(UID), "PGID": str(GID)}
    init = image.run("init-wpbackup-config", **ids)
    assert init.returncode == 0, init.stderr
    crontab = image.schedule_dir(UID) / "crontab"
    assert crontab.read_text(encoding="utf-8").startswith("0 3 * * * wpbackup2 backup")
    assert f"scheduled jobs (as uid {UID}:{GID})" in init.stdout
    service = image.run("svc-wpbackup-schedule", **ids)
    assert service.returncode == 0, service.stderr
    assert image.calls() == [
        f"setpriv {UID}:{GID} l3io-wpbackup-scheduler prepare",
        f"setpriv {UID}:{GID} l3io-wpbackup-scheduler exec",
        f"supercronic as {UID}:{GID}: -passthrough-logs {crontab}",
    ]
    # Nothing that was there changed owner or mode; the one new folder is
    # the schedule's own, private.
    after = [entry for entry in image.owners() if "wpbackup-schedule-" not in entry[0]]
    assert after == before
    assert stat.S_IMODE(image.schedule_dir(UID).stat().st_mode) == 0o700


@AS_ROOT
def test_the_s6_path_with_puid_0_checks_as_the_site_owner(image: Image) -> None:
    ids = {"STUB_UID": "0", "STUB_GID": "0", "PUID": "0", "PGID": "0"}
    init = image.run("init-wpbackup-config", **ids)
    assert init.returncode == 0, init.stderr
    service = image.run("svc-wpbackup-schedule", **ids)
    assert service.returncode == 0, service.stderr
    crontab = image.schedule_dir(0) / "crontab"
    assert image.calls() == [
        f"setpriv {UID}:{GID} l3io-wpbackup-scheduler check",
        f"supercronic as 0:0: -passthrough-logs {crontab}",
    ]


@AS_ROOT
def test_the_s6_path_refuses_an_unwritable_state_folder(image: Image) -> None:
    image.state.chmod(0o500)
    try:
        before = image.owners()
        ids = {"STUB_UID": "0", "STUB_GID": "0", "PUID": str(UID), "PGID": str(GID)}
        init = image.run("init-wpbackup-config", **ids)
        after = image.owners()
    finally:
        image.state.chmod(0o700)
    assert after == before
    assert init.returncode == 3
    wanted = f"state directory {image.state}/l3io-wp-backup is not writable"
    assert wanted in init.stderr
    assert "the scheduled mode was not started" in init.stderr
    assert not image.schedule_dir(UID).exists()


# ---- started as another user (Docker --user) ------------------------------------


@AS_ROOT
def test_a_user_scheduled_start_runs_supercronic_without_s6_or_a_switch(
    image: Image,
) -> None:
    before = image.owners()
    result = image.run("l3io-wpbackup-entrypoint")
    assert result.returncode == 0, result.stderr
    crontab = image.schedule_dir(UID) / "crontab"
    assert image.calls() == [f"supercronic as {UID}:{GID}: -passthrough-logs {crontab}"]
    after = [entry for entry in image.owners() if "wpbackup-schedule-" not in entry[0]]
    assert after == before


def test_a_user_one_shot_runs_as_that_user(image: Image) -> None:
    result = image.run("l3io-wpbackup-entrypoint", "prune", "--keep", "3")
    assert result.returncode == 0, result.stderr
    assert image.calls() == [f"wpbackup2 as {UID}:{GID}: prune --keep 3"]


@AS_ROOT
def test_puid_pgid_naming_the_user_itself_are_accepted(image: Image) -> None:
    result = image.run(
        "l3io-wpbackup-entrypoint", "backup", PUID=str(UID), PGID=str(GID)
    )
    assert result.returncode == 0, result.stderr
    assert image.calls() == [f"wpbackup2 as {UID}:{GID}: backup"]


@AS_ROOT
@pytest.mark.parametrize("args", [(), ("backup",)], ids=["scheduled", "one-shot"])
@pytest.mark.parametrize("which", ["PUID", "PGID"])
def test_a_puid_that_is_not_the_user_is_refused(
    image: Image, args: tuple[str, ...], which: str
) -> None:
    result = image.run("l3io-wpbackup-entrypoint", *args, **{which: "999999"})
    assert result.returncode == 2
    assert f"the container runs as uid {UID}:{GID}" in result.stderr
    assert "cannot switch users" in result.stderr
    assert image.calls() == []


@AS_ROOT
@pytest.mark.parametrize(
    ("folder", "what", "access", "mode"),
    [
        ("state", "state directory", "writable", 0o500),
        ("tmp", "temporary directory (TMPDIR)", "writable", 0o500),
        ("backups", "Archive directory", "writable", 0o555),
        ("html", "Site Root", "readable", 0o300),
    ],
)
def test_a_user_scheduled_start_refuses_a_folder_it_cannot_use(
    image: Image, folder: str, what: str, access: str, mode: int
) -> None:
    path: Path = getattr(image, folder)
    path.chmod(mode)
    try:
        result = image.run("l3io-wpbackup-entrypoint")
    finally:
        path.chmod(0o700)
    assert result.returncode == 3
    err = result.stderr
    assert f"the {what} {path}" in err
    assert f"is not {access} by uid {UID}:{GID}" in err
    assert f"mode {mode:04o}" in err
    assert "wpbackup2 never changes ownership" in err
    assert f"chown -R {UID}:{GID}" in err
    assert "the scheduled mode was not started" in err
    assert image.calls() == []


@AS_ROOT
def test_every_unusable_folder_is_named_at_once(image: Image) -> None:
    image.state.chmod(0o500)
    image.backups.chmod(0o500)
    try:
        result = image.run("l3io-wpbackup-entrypoint")
    finally:
        image.state.chmod(0o700)
        image.backups.chmod(0o700)
    assert result.returncode == 3
    assert "state directory" in result.stderr
    assert "Archive directory" in result.stderr


@AS_ROOT
def test_an_unreadable_wp_config_is_refused_at_start(image: Image) -> None:
    config = image.html / "wp-config.php"
    config.chmod(0o000)
    try:
        result = image.run("l3io-wpbackup-entrypoint")
    finally:
        config.chmod(0o600)
    assert result.returncode == 3
    assert f"wp-config.php {config} is not readable" in result.stderr
    assert image.calls() == []


@AS_ROOT
def test_a_site_root_not_there_yet_is_left_to_each_backup(image: Image) -> None:
    result = image.run(
        "l3io-wpbackup-entrypoint", L3IO_WP_BACKUP_WP_DIR=str(image.data / "later")
    )
    assert result.returncode == 0, result.stderr
    assert "is not there yet" in result.stderr
    assert image.calls()[-1].startswith("supercronic as ")


@AS_ROOT
def test_a_missing_archive_folder_is_checked_where_it_would_be_made(
    image: Image,
) -> None:
    image.data.chmod(0o500)
    try:
        result = image.run(
            "l3io-wpbackup-entrypoint",
            L3IO_WP_BACKUP_ARCHIVE_DIR=str(image.data / "new" / "site"),
        )
    finally:
        image.data.chmod(0o700)
    assert result.returncode == 3
    assert f"checked at {image.data}," in result.stderr
