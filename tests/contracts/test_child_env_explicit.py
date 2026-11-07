"""BL-E002-117 (red team RT-4) — every child this package starts gets an explicit env.

The passphrase stays in ``os.environ`` for the whole Run (owner decision O3,
option (a), 2026-11-06), so the protection is at the other end: no child this
package starts inherits the environment. ``adapters/wpcli.py`` passes
:data:`~l3io.wp.backup.adapters.wpcli.WP_CLI_ENV_KEYS` plus
``WP_CLI_CONFIG_PATH``; ``adapters/preflight.py``'s probes pass ``PATH``,
``HOME`` and ``LC_ALL=C``. That the values hold no secret is
``tests/adapters/test_wpcli.py::test_no_sentinel_reaches_a_wp_child``.

**Scope, derived.** Every ``*.py`` under the package root, found by walking it
— and the walk is checked against ``pkgutil.walk_packages`` over the
installed package, so a module the walk missed fails here.

**Rule.** In each module, every name bound to a process-starting function —
``import subprocess``, ``import subprocess as sp``, ``from subprocess import
run as r``, and the same for ``os`` and ``asyncio`` — is resolved, and:

* a call to ``subprocess.run``/``Popen``/``call``/``check_call``/
  ``check_output`` or ``asyncio.create_subprocess_exec``/``_shell`` must pass
  ``env=`` as a keyword whose value is not the literal ``None``. A ``**``
  splat does not count: what it holds is not visible here;
* ``os.system``, ``os.popen``, ``os.exec*``, ``os.spawn*``, ``os.posix_spawn*``
  and ``subprocess.getoutput``/``getstatusoutput`` are refused outright: the
  first two cannot take an environment, and the rest are not how this
  package starts a child (use ``subprocess`` with ``env=``);
* any of them used as a **value** rather than called — ``f = subprocess.run``,
  a default argument, a callback — is refused, because the call it later
  makes is invisible to this scan. ``adapters/wpcli.py`` injects a wrapper
  that itself calls ``subprocess.run(..., env=env)``, not the function.

An ``env=`` whose expression reads ``os.environ``, ``os.environb`` or
``os.getenv`` (``env=os.environ``, ``{**os.environ}``, ``os.environ.copy()``)
is refused as inheritance. Star imports from those modules are refused, and
``import os.path`` is read as binding ``os``.

**Blind spots, stated.** An environment built from ``os.environ`` in another
function or variable and passed by name (``env=built``) — only the call's own
expression is inspected, so the values are the leak test's
(``tests/adapters/test_wpcli.py``, which runs the real ``run_child``);
``getattr(subprocess, "run")`` and ``importlib`` lookups; ``multiprocessing``
and ``pty``; and every child started inside a dependency — notably
``l3io-wordpress-database``'s own ``mysqldump``, ``mysql`` and ``age``
children, which inherit the process environment and run no site code (the
residual recorded on BL-E002-117).
"""

from __future__ import annotations

import ast
import pkgutil
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

import l3io.wp.backup as package

ROOT = Path(package.__file__).parent

#: Must be called with an explicit, non-None ``env=`` keyword.
NEEDS_ENV = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
#: Refused outright (prefix match for the ``os`` families).
REFUSED_PREFIXES = (
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
)
MODULES = ("subprocess", "os", "asyncio")
#: Reading these into ``env=`` is inheritance under another name.
INHERITED = frozenset({"os.environ", "os.environb", "os.getenv"})


def _targeted(qualified: str) -> bool:
    return qualified in NEEDS_ENV or qualified.startswith(REFUSED_PREFIXES)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def _aliases(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, str], list[int]]:
    """``{local name: module}``, ``{local name: module.function}``, star lines.

    ``import os.path`` binds ``os``, so a dotted import binds its first part.
    ``from subprocess import *`` cannot be resolved name by name, so it is
    returned as a line to refuse.
    """
    modules: dict[str, str] = {}
    functions: dict[str, str] = {}
    stars: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.asname is None and top in MODULES:
                    modules[top] = top
                elif alias.name in MODULES:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in MODULES:
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node.lineno)
                    continue
                qualified = f"{node.module}.{alias.name}"
                if _targeted(qualified) or qualified in INHERITED:
                    functions[alias.asname or alias.name] = qualified
    return modules, functions, stars


def _qualified(
    node: ast.AST, modules: dict[str, str], functions: dict[str, str]
) -> str | None:
    if isinstance(node, ast.Name) and node.id in functions:
        return functions[node.id]
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    ):
        qualified = f"{modules[node.value.id]}.{node.attr}"
        return qualified if _targeted(qualified) or qualified in INHERITED else None
    return None


def _inherits(
    value: ast.AST, modules: dict[str, str], functions: dict[str, str]
) -> bool:
    """True when ``value`` reads the process environment (``os.environ`` & co)."""
    for node in ast.walk(value):
        if isinstance(node, ast.Name) and functions.get(node.id) in INHERITED:
            return True
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and f"{modules.get(node.value.id)}.{node.attr}" in INHERITED
        ):
            return True
    return False


def _env_problem(
    call: ast.Call, modules: dict[str, str], functions: dict[str, str]
) -> str | None:
    """Why ``call``'s environment is not explicit and minimal, or ``None``."""
    for keyword in call.keywords:
        if keyword.arg == "env":
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is None:
                return "env=None"
            if _inherits(value, modules, functions):
                return "an env= built from the process environment"
            return None
    return "no explicit env="


def violations(source: str, path: str) -> list[Violation]:
    """The rule over one module's source."""
    tree = ast.parse(source)
    modules, functions, stars = _aliases(tree)
    called: set[int] = set()
    found = [
        Violation(path, line, "a star import from a process module") for line in stars
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = _qualified(node.func, modules, functions)
        if qualified is None:
            continue
        called.add(id(node.func))
        if qualified not in NEEDS_ENV:
            found.append(Violation(path, node.lineno, f"{qualified}() is refused"))
        elif (problem := _env_problem(node, modules, functions)) is not None:
            found.append(Violation(path, node.lineno, f"{qualified}(): {problem}"))
    for node in ast.walk(tree):
        if id(node) in called or isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        qualified = _qualified(node, modules, functions)
        if qualified in INHERITED:
            continue
        if qualified is not None and isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            found.append(
                Violation(path, node.lineno, f"{qualified} used as a value, not called")
            )
    return found


def _walked(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def scan(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for path in _walked(root):
        rel = path.relative_to(root).as_posix()
        found += violations(path.read_text(encoding="utf-8"), rel)
    return found


# --------------------------------------------------------------------------
# The rule over the package
# --------------------------------------------------------------------------


def test_every_child_process_gets_an_explicit_env() -> None:
    found = scan(ROOT)
    assert not found, "\n".join(map(str, found))


def test_the_walk_covers_every_importable_module() -> None:
    """Scope attack: the walked file set is the package's module set."""
    walked = {
        ".".join(p.relative_to(ROOT).with_suffix("").parts) for p in _walked(ROOT)
    }
    walked = {m.removesuffix(".__init__").removesuffix("__init__") for m in walked}
    importable = {
        info.name.removeprefix(f"{package.__name__}.")
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
    }
    assert importable, "pkgutil found no module"
    assert importable <= walked, sorted(importable - walked)


def test_the_package_does_start_children_so_the_scan_is_live() -> None:
    """A scan that matched nothing anywhere would pass vacuously."""
    hits = 0
    for path in _walked(ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, functions, _ = _aliases(tree)
        hits += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _qualified(node.func, modules, functions) in NEEDS_ENV
        )
    assert hits >= 2, "expected adapters/preflight.py and adapters/wpcli.py"


# --------------------------------------------------------------------------
# Planted violations: the rule, the aliases, and the scope
# --------------------------------------------------------------------------

PLANTED = {
    "no-env": "import subprocess\nsubprocess.run(['x'])\n",
    "env-none": "import subprocess\nsubprocess.run(['x'], env=None)\n",
    "splat": "import subprocess\nkw = {}\nsubprocess.Popen(['x'], **kw)\n",
    "module-alias": "import subprocess as sp\nsp.check_output(['x'])\n",
    "from-alias": "from subprocess import run as r\nr(['x'])\n",
    "os-system": "import os\nos.system('x')\n",
    "os-execv": "import os\nos.execv('/bin/x', ['x'])\n",
    "os-spawn-from": "from os import spawnlp\nspawnlp(0, 'x', 'x')\n",
    "asyncio": (
        "import asyncio\n"
        "async def f():\n    await asyncio.create_subprocess_exec('x')\n"
    ),
    "value": "import subprocess\ndef f(run=subprocess.run):\n    return run\n",
    "value-from": "from subprocess import Popen\nstart = Popen\n",
    "getoutput": "import subprocess\nsubprocess.getoutput('x')\n",
    "env-environ": "import os, subprocess\nsubprocess.run(['x'], env=os.environ)\n",
    "env-splat-environ": (
        "import os, subprocess\nsubprocess.run(['x'], env={**os.environ, 'A': 'b'})\n"
    ),
    "env-copy": "import os, subprocess\nsubprocess.run(['x'], env=os.environ.copy())\n",
    "env-from-environ": (
        "from os import environ\nimport subprocess\n"
        "subprocess.run(['x'], env=environ)\n"
    ),
    "env-getenv": (
        "import os, subprocess\nsubprocess.run(['x'], env={'P': os.getenv('P')})\n"
    ),
    "star": "from subprocess import *\nrun(['x'])\n",
    "dotted-import": "import os.path\nos.system('x')\n",
}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_violation_is_found(name: str) -> None:
    assert violations(PLANTED[name], f"{name}.py"), name


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['x'], env={'PATH': '/bin'})\n",
        "import subprocess\nenv = {}\nsubprocess.run(['x'], env=env)\n",
        "from subprocess import run\nrun(['x'], env=dict())\n",
        "import subprocess\nsubprocess.DEVNULL\nsubprocess.CompletedProcess\n",
        "import os\nos.environ.get('PATH')\n",
    ],
)
def test_the_compliant_forms_pass(source: str) -> None:
    assert violations(source, "ok.py") == []


def test_a_violation_in_a_new_module_is_found(tmp_path: Path) -> None:
    """Scope attack: a module nobody listed, in a new subpackage, is scanned."""
    root = tmp_path / "pkg"
    new = root / "adapters" / "brand_new"
    new.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (new / "child.py").write_text(
        textwrap.dedent(
            """
            from subprocess import run as start

            def go() -> None:
                start(["wp", "cli", "version"])
            """
        ),
        encoding="utf-8",
    )
    found = scan(root)
    assert [(v.path, v.line) for v in found] == [("adapters/brand_new/child.py", 5)]
