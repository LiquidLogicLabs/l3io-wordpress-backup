"""ADR-0014/ADR-0024 — no cipher in this package (E004-S01-003).

``age`` is reached only through ``l3io-wordpress-database``'s
``encrypt``/``decrypt``. An AST scan of every ``*.py`` under
``src/l3io/wp/backup/`` (``rglob``, with a scope assertion) fails on:

- any import of ``pyrage`` or ``cryptography.hazmat`` (``import x``,
  ``import x as y``, ``from x import y``, ``from x.sub import y``);
- a ``subprocess`` or ``os`` process call whose positional or keyword
  argument holds a string constant naming ``age`` or ``age-plugin-batchpass``,
  bare or as a path (a list literal's element, or the argument itself).

Not covered: a program name assembled at runtime, or reached through a
variable; ``importlib.import_module("pyrage")`` is caught only as a call
argument constant, which this scan does not look for.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"
FORBIDDEN = ("pyrage", "cryptography.hazmat")
PROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "execv", "execvp"}
    | {"execvpe", "execve", "spawnv", "spawnvp", "posix_spawn", "posix_spawnp"}
)
AGE_PROGRAMS = frozenset({"age", "age-plugin-batchpass"})


def _forbidden(module: str | None) -> bool:
    return module is not None and any(
        module == name or module.startswith(f"{name}.") for name in FORBIDDEN
    )


def _names_age(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        # A bare name or a path to the binary (``/usr/local/bin/age``).
        return (
            isinstance(node.value, str)
            and PurePosixPath(node.value).name in AGE_PROGRAMS
        )
    if isinstance(node, ast.List | ast.Tuple):
        return any(_names_age(element) for element in node.elts)
    return False


def _offenders(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
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
        "subprocess.Popen(['/usr/local/bin/age', '-d'])\n",
        encoding="utf-8",
    )
    (tmp_path / "fine.py").write_text(
        "import cryptography\nfrom cryptography.x509 import load_pem_x509_certificate\n"
        "import subprocess\nsubprocess.run(['mysqldump', '--age'])\n",
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
    ]
