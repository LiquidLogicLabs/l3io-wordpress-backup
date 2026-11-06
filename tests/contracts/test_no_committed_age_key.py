"""ADR-0024 — no ``age`` secret key is committed (E004-S01-003).

The committed fixture's passphrase protects only that fixture. A real key
committed by mistake would protect real Archives, so every file ``git ls-files``
lists is scanned, as bytes, for the full ``age`` secret-key forms:
``AGE-SECRET-KEY-1`` (classic) or ``AGE-SECRET-KEY-PQ-1`` (post-quantum,
age 1.3) followed by at least 58 characters of ``[0-9A-Z]``. The scope is
git's own list (a new file is in it when it is tracked), with a scope
assertion. Not covered: a key split across lines, encoded, or in an untracked
file.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: Built from parts, so this file holds no full key form itself.
PREFIX = "AGE-SECRET-" + "KEY-1"
PQ_PREFIX = "AGE-SECRET-" + "KEY-PQ-1"
KEY = re.compile(rb"AGE-SECRET-" + rb"KEY-(?:PQ-)?1[0-9A-Z]{58,}")


def _tracked() -> list[Path]:
    git = shutil.which("git")
    if git is None or not (PROJECT_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    listed = subprocess.run(  # noqa: S603 - fixed program and arguments
        [git, "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [PROJECT_ROOT / name for name in listed.decode().split("\0") if name]


def _keys_in(paths: list[Path]) -> list[str]:
    found = []
    for path in paths:
        if path.is_file() and not path.is_symlink() and KEY.search(path.read_bytes()):
            found.append(str(path))
    return found


def test_the_scope_is_the_tracked_tree() -> None:
    tracked = _tracked()
    assert PROJECT_ROOT / "pyproject.toml" in tracked
    assert (
        PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "adapters" / "crypto.py"
        in tracked
    )


def test_no_tracked_file_holds_an_age_secret_key() -> None:
    found = _keys_in(_tracked())
    assert not found, f"an age secret key is committed: {found}"


def test_a_planted_key_is_found(tmp_path: Path) -> None:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    body = "".join(secrets.choice(alphabet) for _ in range(58))
    planted = PREFIX + body
    (tmp_path / "keys.txt").write_text(f"# created\n{planted}\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe" + planted.encode() + b"\x00")
    (tmp_path / "short.txt").write_text(planted[:-1] + "\n", encoding="utf-8")
    (tmp_path / "pq.txt").write_text(f"{PQ_PREFIX}{body}\n", encoding="utf-8")
    (tmp_path / "pq-short.txt").write_text(
        f"{PQ_PREFIX}{body[:-1]}\n", encoding="utf-8"
    )
    found = _keys_in(sorted(tmp_path.iterdir()))
    assert found == [
        str(tmp_path / "binary.bin"),
        str(tmp_path / "keys.txt"),
        str(tmp_path / "pq.txt"),
    ]


def test_this_file_holds_no_full_key_form() -> None:
    assert not KEY.search(Path(__file__).read_bytes())
