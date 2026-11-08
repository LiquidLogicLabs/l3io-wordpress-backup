"""ADR-0014/ADR-0024 — no cipher in this package (E004-S01-003).

``age`` is reached only through ``l3io-wordpress-database``'s
``encrypt``/``decrypt``. An AST scan of every ``*.py`` under
``src/l3io/wp/backup/`` (``rglob``, with a scope assertion) fails on:

- any import of ``pyrage`` or of the whole ``cryptography`` package, not only
  ``cryptography.hazmat`` (``import x``, ``import x as y``, ``from x import
  y`` -- so ``from cryptography import hazmat`` and ``cryptography.fernet`` --
  and ``from x.sub import y``);
- a process call -- ``subprocess`` ``run/Popen/call/check_call/check_output``,
  ``os`` ``exec*/spawn*/posix_spawn*/system/popen``, ``asyncio``
  ``create_subprocess_exec/shell`` -- matched by the called name, whatever
  object it is called on, whose positional or keyword argument holds a string
  constant whose first word names ``age`` or ``age-plugin-batchpass``, bare or
  as a path (a list literal's element, the argument itself, or the program at
  the head of a shell command line such as ``"age -d"``).

Not covered: a program name assembled at runtime, or reached through a
variable, an f-string or a concatenation; an argument list built elsewhere
and passed by name; ``importlib.import_module("pyrage")`` and
``__import__("pyrage")`` (call arguments, not imports); ``ctypes`` or
``cffi`` loading a crypto library; ``shlex``-quoted command lines whose first
word is quoted; another process API (``pty``, ``multiprocessing``,
``pexpect``).

**The forbidden imports are a hand-kept list** (``FORBIDDEN``): there is no
source of truth naming every Python crypto library to derive it from. A crypto
library other than those named (``nacl`` or ``Crypto``, say) is not caught
until it is added here. Accepted as a stated limit
(owner decision 2026-11-07, BL-E004-067); review covers a new dependency, and
``pyproject.toml``'s dependency list is short enough to read.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
FORBIDDEN = ("pyrage", "cryptography")
PROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "execv", "execvp"}
    | {"execvpe", "execve", "spawnv", "spawnvp", "posix_spawn", "posix_spawnp"}
    | {"system", "popen", "create_subprocess_exec", "create_subprocess_shell"}
)
AGE_PROGRAMS = frozenset({"age", "age-plugin-batchpass"})


def _forbidden(module: str | None) -> bool:
    return module is not None and any(
        module == name or module.startswith(f"{name}.") for name in FORBIDDEN
    )


def _names_age(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        # A bare name, a path to the binary (``/usr/local/bin/age``) or the
        # head of a shell command line (``age -d``).
        words = node.value.split() if isinstance(node.value, str) else []
        return bool(words) and PurePosixPath(words[0]).name in AGE_PROGRAMS
    if isinstance(node, ast.List | ast.Tuple):
        return any(_names_age(element) for element in node.elts)
    return False


def _offenders(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
            where = f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Import):
                found += [
                    f"{where}: import {a.name}"
                    for a in node.names
                    if _forbidden(a.name)
                ]
            elif isinstance(node, ast.ImportFrom) and _forbidden(node.module):
                found.append(f"{where}: from {node.module}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute | ast.Name)
                and (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                )
                in PROCESS_CALLS
                and any(
                    _names_age(arg)
                    for arg in (*node.args, *(k.value for k in node.keywords))
                )
            ):
                found.append(f"{where}: runs age")
    return found


def test_the_walk_reaches_the_cipher_adapter() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert PACKAGE / "adapters" / "crypto.py" in files, "scope missing crypto.py"


def test_the_package_holds_no_direct_crypto() -> None:
    offenders = _offenders(PACKAGE)
    assert not offenders, f"ADR-0014 — go through l3io-wp-database: {offenders}"


def test_planted_crypto_is_found_however_deep_and_however_spelled(
    tmp_path: Path,
) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "leak.py").write_text(
        "import pyrage as p\n"
        "from cryptography.hazmat.primitives import ciphers\n"
        "import cryptography.hazmat\n"
        "import subprocess\n"
        "subprocess.run(['age', '-d'])\n"
        "from subprocess import Popen\n"
        "Popen(('age-plugin-batchpass',))\n"
        "subprocess.run(args=['age', '-p'])\n"
        "subprocess.Popen(['/usr/local/bin/age', '-d'])\n"
        "from cryptography import hazmat\n"
        "from cryptography.fernet import Fernet\n"
        "import cryptography\n"
        "import os\n"
        "os.system('age -d')\n"
        "os.popen('/usr/bin/age -p')\n"
        "import asyncio\n"
        "asyncio.create_subprocess_exec('age', '-d')\n"
        "asyncio.create_subprocess_shell('age-plugin-batchpass')\n"
        "from os import system\n"
        "system('age')\n",
        encoding="utf-8",
    )
    (tmp_path / "fine.py").write_text(
        "import subprocess\nsubprocess.run(['mysqldump', '--age'])\n"
        "import os\nos.system('mysqldump --age')\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == [
        "a/b/leak.py:1: import pyrage",
        "a/b/leak.py:2: from cryptography.hazmat.primitives",
        "a/b/leak.py:3: import cryptography.hazmat",
        "a/b/leak.py:5: runs age",
        "a/b/leak.py:7: runs age",
        "a/b/leak.py:8: runs age",
        "a/b/leak.py:9: runs age",
        "a/b/leak.py:10: from cryptography",
        "a/b/leak.py:11: from cryptography.fernet",
        "a/b/leak.py:12: import cryptography",
        "a/b/leak.py:14: runs age",
        "a/b/leak.py:15: runs age",
        "a/b/leak.py:17: runs age",
        "a/b/leak.py:18: runs age",
        "a/b/leak.py:20: runs age",
    ]
