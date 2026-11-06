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
import shutil
import signal
import subprocess
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
