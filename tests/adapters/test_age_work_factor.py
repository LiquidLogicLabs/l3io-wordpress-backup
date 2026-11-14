"""A busy host encrypts and authenticates exactly as an idle one (2026-11-13).

``l3io-age-passphrase`` 0.1.2 left age's scrypt work factor to age's
defaults, which time a small scrypt run on the wall clock: encryption picked
the work factor from that timing, and decryption refused any file above it
plus 4. On a loaded host the timing runs long, so ``verify --authenticate``
refused a good Archive written while the host was idle, as
``PayloadDamagedError`` (exit 8, ``member_decryption_failed`` ``corrupt``),
and an Archive written while the host was busy got a weak work factor. Seen in
``tests/integration/test_root_backup.py`` under a local act run.

Each test drives :class:`PassphraseCipher` (the binder backend) in a child
process that, for its first seconds, runs about 0.5 ms in every 100 ms, so any
wall-clock measurement it takes is over a hundred times too long (log2: about
7) whatever the host's speed. The binder that fixes this is not released yet:
until then these tests are strict xfails on 0.1.2 and below, and must pass on
anything newer.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from packaging.version import Version

pytest.importorskip("l3io.wp.database")

PASSPHRASE = "test-passphrase-not-a-secret"
#: What ``STOCK_AGE`` decrypts to.
PLAIN = b"work factor\n" * 100
#: ``PLAIN`` as stock ``age -p`` (1.2.1) encrypted it with ``PASSPHRASE``.
STOCK_AGE = Path(__file__).parents[1] / "data" / "age" / "stock-age-p-wf18.age"
#: Stock ``age -p``'s scrypt work factor, which the fixed binder writes too.
WORK_FACTOR = 18
_STANZA = re.compile(rb"^-> scrypt \S+ (\d+)$", re.MULTILINE)
#: The last binder release with the load-dependent work factor.
_UNFIXED = Version("0.1.2")

pytestmark = pytest.mark.xfail(
    Version(importlib.metadata.version("l3io-age-passphrase")) <= _UNFIXED,
    reason="l3io-age-passphrase <= 0.1.2 times scrypt on the wall clock",
    strict=True,
)

_CHILD = r"""
import io, os, resource, sys
os.environ["L3IO_AGE_PASSPHRASE_BACKEND"] = "binder"
from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.domain.errors import BackupError
resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
mode, path, passphrase = sys.argv[1:4]
cipher = PassphraseCipher(passphrase)
cipher.check()  # imports database and the binder now, before the throttle
data = open(path, "rb").read()
sys.stdout.write("ready\n"); sys.stdout.flush()
sys.stdin.readline()
out = io.BytesIO()
try:
    getattr(cipher, mode)(io.BytesIO(data), out)
except BackupError as exc:
    sys.stdout.write(type(exc).__name__ + "\n")
    sys.exit(3)
open(path + ".out", "wb").write(out.getvalue())
"""


def _throttled(mode: str, source: Path, *, seconds: float = 4.0) -> bytes:
    """``cipher.<mode>`` of ``source`` in a child stopped > 99% of ``seconds``."""
    child = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", _CHILD, mode, str(source), PASSPHRASE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    try:
        assert child.stdout.readline() == "ready\n"
        # Stopped before it is told to go, so it never runs unthrottled.
        os.kill(child.pid, signal.SIGSTOP)
        child.stdin.write("go\n")
        child.stdin.flush()
        end = time.monotonic() + seconds
        while time.monotonic() < end and child.poll() is None:
            # About 0.5 ms running (a SIGSTOP sent at once would cancel the
            # SIGCONT before the child ran at all), then 100 ms stopped.
            os.kill(child.pid, signal.SIGCONT)
            time.sleep(0.0005)
            os.kill(child.pid, signal.SIGSTOP)
            time.sleep(0.1)
    except ProcessLookupError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child.pid, signal.SIGCONT)
    output, _ = child.communicate(timeout=120)
    assert child.returncode == 0, output
    return Path(f"{source}.out").read_bytes()


def _work_factor(sealed: bytes) -> int:
    found = _STANZA.findall(sealed)
    assert len(found) == 1, found
    return int(found[0])


def test_a_busy_host_authenticates_what_an_idle_host_encrypted(tmp_path: Path) -> None:
    source = tmp_path / "payload.age"
    source.write_bytes(STOCK_AGE.read_bytes())
    assert _work_factor(source.read_bytes()) == WORK_FACTOR
    assert _throttled("decrypt", source) == PLAIN


def test_a_busy_host_encrypts_at_the_full_work_factor(tmp_path: Path) -> None:
    source = tmp_path / "payload"
    source.write_bytes(PLAIN)
    sealed = _throttled("encrypt", source)
    assert _work_factor(sealed) == WORK_FACTOR
