"""ADR-0024, FR-4, Gate E004 AG-4 — stock ``age`` and this package agree (E004-S01-003).

An Archive's payload must be recoverable without this package: a payload the
adapter encrypted decrypts with ``age -d``, and a file ``age -p`` encrypted
decrypts through the adapter. The passphrase reaches ``age`` the only way it
accepts one, at its terminal prompt, driven here through a ``pty``; it is
never an argument. Both of database's backends run: the
``l3io-age-passphrase`` binder and the ``age`` CLI with ``age-plugin-batchpass``.

CI installs a pinned ``age`` 1.3.2 (``.github/workflows/ci.yml``, "install
age"), so with ``CI`` set a missing binary, or an ``age`` below 1.3, **fails**
rather than skips. Elsewhere the module skips, saying why.
"""

from __future__ import annotations

import io
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import termios
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.crypto import PassphraseCipher

BACKEND_ENV = "L3IO_AGE_PASSPHRASE_BACKEND"
MINIMUM = (1, 3)
#: Each prompt names the passphrase and ends at a colon.
PROMPT = re.compile(rb"passphrase[^:\n]*:", re.IGNORECASE)
TIMEOUT_S = 60.0
#: How long ``age`` may take, after printing a prompt, to switch echo off.
ECHO_OFF_TIMEOUT_S = 10.0


def _age_version() -> tuple[int, ...] | None:
    binary = shutil.which("age")
    if binary is None or shutil.which("age-plugin-batchpass") is None:
        return None
    completed = subprocess.run(  # noqa: S603 - the located age binary
        [binary, "--version"], capture_output=True, text=True, check=False
    )
    found = re.search(r"(\d+)\.(\d+)", completed.stdout)
    return None if found is None else (int(found[1]), int(found[2]))


def _require_age() -> str:
    version = _age_version()
    if version is not None and version >= MINIMUM:
        age = shutil.which("age")
        assert age is not None
        return age
    reason = (
        "needs age >= 1.3 and age-plugin-batchpass on PATH "
        f"(found {version or 'none'}); CI installs age 1.3.2"
    )
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def age() -> str:
    return _require_age()


@pytest.fixture(params=["binder", "cli"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    value: str = request.param
    monkeypatch.setenv(BACKEND_ENV, value)
    return value


@pytest.fixture
def passphrase() -> str:
    return f"interop-pp-{uuid.uuid4().hex}"


def _echo_is_on(fd: int) -> bool:
    """The terminal's ECHO flag; on Linux the master reports the slave's termios."""
    return bool(termios.tcgetattr(fd)[3] & termios.ECHO)


def _await_echo_off(pid: int, fd: int, seen: bytes) -> None:
    """Block until ``age`` has switched echo off, or kill it and fail.

    ``age`` prints its prompt and only then turns echo off. Typing in that gap
    has the kernel's line discipline echo the passphrase, which is the test
    racing ``age`` and not ``age`` leaking it. Waiting closes the gap without
    hiding a real echo: the assertion on the output still sees anything shown
    once echo is off.
    """
    deadline = time.monotonic() + ECHO_OFF_TIMEOUT_S
    while _echo_is_on(fd):
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail(
                f"age prompted but left echo on for {ECHO_OFF_TIMEOUT_S}s: {seen!r}"
            )
        time.sleep(0.001)


def _drive_age(argv: list[str], passphrase: str) -> tuple[int, bytes]:
    """Run ``age`` on a fresh terminal, answering every passphrase prompt."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs age
        os.execv(argv[0], argv)  # noqa: S606 - the located age binary
    seen, answered = b"", 0
    deadline = time.monotonic() + TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            seen += chunk
            while len(PROMPT.findall(seen)) > answered:
                _await_echo_off(pid, fd, seen)
                os.write(fd, passphrase.encode() + b"\n")
                answered += 1
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == 0:  # still running at the deadline: never hang the suite
            deadline = time.monotonic() + 5
            while done == 0 and time.monotonic() < deadline:
                time.sleep(0.1)
                done, status = os.waitpid(pid, os.WNOHANG)
            if done == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                pytest.fail(f"age did not finish: {seen!r}")
    finally:
        os.close(fd)
    return os.waitstatus_to_exitcode(status), seen


@pytest.mark.usefixtures("backend")
def test_stock_age_decrypts_what_the_adapter_encrypted(
    age: str, tmp_path: Path, passphrase: str
) -> None:
    plain = os.urandom(200 * 1024)
    sealed = tmp_path / "payload.tar.gz.age"
    with sealed.open("wb") as out:
        PassphraseCipher(passphrase).encrypt(io.BytesIO(plain), out)
    recovered = tmp_path / "recovered"
    code, shown = _drive_age([age, "-d", "-o", str(recovered), str(sealed)], passphrase)
    assert code == 0, shown
    assert recovered.read_bytes() == plain
    assert passphrase.encode() not in shown


@pytest.mark.usefixtures("backend")
def test_the_adapter_decrypts_what_age_p_encrypted(
    age: str, tmp_path: Path, passphrase: str
) -> None:
    plain = os.urandom(200 * 1024)
    source, sealed = tmp_path / "plain", tmp_path / "sealed.age"
    source.write_bytes(plain)
    code, shown = _drive_age([age, "-p", "-o", str(sealed), str(source)], passphrase)
    assert code == 0, shown
    out = io.BytesIO()
    with sealed.open("rb") as reader:
        PassphraseCipher(passphrase).decrypt(reader, out)
    assert out.getvalue() == plain


def test_the_passphrase_never_reaches_an_argument_vector(
    age: str, tmp_path: Path, passphrase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the ``cli`` backend database runs ``age``: no argv holds the secret."""
    monkeypatch.setenv(BACKEND_ENV, "cli")
    seen: list[list[str]] = []
    real = subprocess.Popen.__init__

    def spy(
        self: subprocess.Popen[bytes], args: object, *a: object, **k: object
    ) -> None:
        seen.append(
            [str(arg) for arg in args] if isinstance(args, list) else [str(args)]
        )
        real(self, args, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    out = io.BytesIO()
    PassphraseCipher(passphrase).encrypt(io.BytesIO(b"payload"), out)
    assert seen, "the cli backend ran no age process"
    assert all(passphrase not in " ".join(argv) for argv in seen)
    assert age  # the binary the module requires is the one database ran


#: The whole environment database 0.3.3 gives ``age`` (BL-E004-055).
AGE_CHILD_KEYS = frozenset({"PATH", "HOME", "LC_ALL", "AGE_PASSPHRASE_FD"})


def _recording_age(shim: Path, records: Path, real_age: str) -> None:
    """An ``age`` on ``PATH`` that saves its own environment, then execs ``real_age``.

    It copies ``/proc/$$/environ``: the environment the kernel handed this
    process at ``execve``, before the shell could add or drop anything, and
    the same one ``exec`` passes to the real ``age``. Every path is literal,
    because the environment it records is the one under test.
    """
    cat = shutil.which("cat")
    assert cat is not None
    shim.mkdir()
    script = shim / "age"
    script.write_text(
        "#!/bin/sh\n"
        f'{shlex.quote(cat)} "/proc/$$/environ"'
        f' > {shlex.quote(str(records))}/"$$.env"\n'
        f'exec {shlex.quote(real_age)} "$@"\n'
    )
    script.chmod(0o755)


def test_the_age_child_gets_only_its_own_environment(
    age: str, tmp_path: Path, passphrase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E004-055: the parent's secrets never reach the ``age`` child.

    The parent holds the database password variable, another planted secret
    and a relative ``PATH`` entry. A recording ``age`` shim, first on
    ``PATH``, saves what the child really received, for an encryption and a
    decryption under the ``cli`` backend. Against database 0.3.2, which passed
    the whole environment, the planted variables are in the record and this
    fails.
    """
    if not Path("/proc/self/environ").is_file():
        pytest.skip("needs /proc/<pid>/environ")
    records, shim = tmp_path / "records", tmp_path / "shim"
    records.mkdir()
    _recording_age(shim, records, str(Path(age).resolve()))
    db_secret = f"db-password-sentinel-{uuid.uuid4().hex}"
    planted = f"planted-sentinel-{uuid.uuid4().hex}"
    monkeypatch.setenv("L3IO_WP_BACKUP_DB_PASSWORD", db_secret)
    monkeypatch.setenv("L3IO_TEST_PLANTED_SECRET", planted)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(shim), "relative-bin", os.environ["PATH"]])
    )
    monkeypatch.setenv(BACKEND_ENV, "cli")

    sealed, recovered = io.BytesIO(), io.BytesIO()
    PassphraseCipher(passphrase).encrypt(io.BytesIO(b"payload"), sealed)
    sealed.seek(0)
    PassphraseCipher(passphrase).decrypt(sealed, recovered)
    assert recovered.getvalue() == b"payload"

    seen = sorted(records.glob("*.env"))
    # The guard's reach: both runs went through the shim, so both were examined.
    assert len(seen) == 2, [p.name for p in seen]
    for record in seen:
        env = dict(
            entry.split("=", 1) for entry in record.read_text().split("\0") if entry
        )
        assert set(env) <= AGE_CHILD_KEYS, sorted(set(env) - AGE_CHILD_KEYS)
        assert env["LC_ALL"] == "C"
        assert all(Path(p).is_absolute() for p in env["PATH"].split(os.pathsep))
        blob = record.read_text()
        for secret in (db_secret, planted, passphrase):
            assert secret not in blob
