"""AD-6 — ``domain/archive_format.py`` is the one owner of the archive format.

No other module under ``src/`` may spell a member name or a role prefix: every
string constant (f-string parts included) is checked against the literals
below. Scope is ``src/`` walked for ``*.py`` files, asserted to contain the
owner, and the walker is run over a planted tree that hides the literal in a
nested directory and inside an f-string.

Not covered, stated so the next reader keeps looking: a literal assembled at
runtime (``"site" + "-root/"``) and the Manifest's key set when spelled in
another module; review covers those.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
OWNER = SRC / "l3io" / "wp" / "backup" / "domain" / "archive_format.py"

#: Member names, matched exactly.
NAMES = ("manifest.json", "payload.tar.gz")
#: Role prefixes, matched at the start of a constant.
PREFIXES = ("dump/", "site-root/", "config/")


def _offenders(files: list[Path], owner: Path) -> list[str]:
    found = []
    for path in files:
        if path == owner:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if value.startswith(PREFIXES) or any(name in value for name in NAMES):
                found.append(f"{path.name}:{node.lineno}: {value!r}")
    return found


def test_the_owner_is_in_scope() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert OWNER in files, f"AD-6 scope missing the owner: {OWNER}"


def test_only_the_owner_spells_a_member_name() -> None:
    offenders = _offenders(sorted(SRC.rglob("*.py")), OWNER)
    assert not offenders, (
        f"AD-6 — import these from domain/archive_format.py: {offenders}"
    )


def test_the_grep_finds_literals_hidden_deep_and_in_f_strings(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "leak.py").write_text(
        "X = 'manifest.json'\n"
        "def name(rel):\n"
        "    return f'site-root/{rel}'\n"
        "Y = 'payload.tar.gz.age'\n",
        encoding="utf-8",
    )
    owner = tmp_path / "archive_format.py"
    owner.write_text("Z = 'manifest.json'\n", encoding="utf-8")
    found = _offenders(sorted(tmp_path.rglob("*.py")), owner)
    assert sorted(found) == [
        "leak.py:1: 'manifest.json'",
        "leak.py:3: 'site-root/'",
        "leak.py:4: 'payload.tar.gz.age'",
    ]
