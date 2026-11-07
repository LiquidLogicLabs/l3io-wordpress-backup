"""AD-29 — existence is the administrator's answer, never a failed login's.

A WordPress account's own login failure (MySQL ``1045``,
``AuthenticationFailedError``) cannot tell "database absent" from "account
absent", and its access-denied failure (MySQL ``1044``, ``AccessDeniedError``)
is returned whether or not the database exists (BL-E003-052), so no
database- or account-existence fact may be derived from either
(FR-9, E003-S02-004). Existence comes only from database's ``survey`` as the
administrator, through ``WpDatabase.survey_target``.

**What it sees.** An AST scan of every ``*.py`` under the package. A handler
is *authentication-shaped* when its ``except`` clause names a class whose
name contains ``AuthenticationFailed`` or ``AccessDenied`` (bare, dotted, or
in a tuple), or its body compares with the constant ``1045`` or ``1044``; an
``if`` whose test compares with either is the same. Inside such a block it
fails on an *existence fact*: an assignment or keyword argument, or a dict
key, whose name contains ``exist``; a ``DatabaseTargetState(...)``
construction; or any ``return`` from a function whose name contains ``exist``.

**What it does not see — this is a heuristic, not a proof.** A fact derived
in a helper the handler calls; a flag named without ``exist`` and turned into
existence later; a ``1045``/``1044`` held in a named constant; a login failure caught
as a broad ``Exception``. The integration tests in
``tests/integration/test_database_restore_report.py`` (a fresh server and an
account-only server, each reported from the administrator's survey) are the
real proof.

**Scope, derived.** The walked package, asserted non-empty and to reach
``adapters/database.py`` (which catches ``AuthenticationFailedError`` today,
only to raise ``CredentialsError``), ``adapters/staging.py`` and
``core/restore.py``. The planted cases attack the scope — a nested module, a
tuple handler, an errno test — as well as the rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup"

#: Login and access-denied failures: neither can tell absent from forbidden.
_AUTH_CLASSES = ("AuthenticationFailed", "AccessDenied")
_LOGIN_ERRNOS = frozenset({1045, 1044})
_EXISTENCE = "exist"
_STATE_TYPE = "DatabaseTargetState"


def _names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Name | ast.Attribute)
    }


def _mentions_errno(nodes: list[ast.stmt] | ast.expr) -> bool:
    roots = nodes if isinstance(nodes, list) else [nodes]
    return any(
        isinstance(n, ast.Compare)
        and any(
            isinstance(c, ast.Constant) and c.value in _LOGIN_ERRNOS
            for c in ast.walk(n)
        )
        for root in roots
        for n in ast.walk(root)
    )


def _auth_blocks(tree: ast.Module) -> list[tuple[list[ast.stmt], str]]:
    """Each authentication-shaped block, with its enclosing function's name."""
    blocks: list[tuple[list[ast.stmt], str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        if isinstance(node, ast.ExceptHandler) and (
            any(cls in name for cls in _AUTH_CLASSES for name in _names(node.type))
            or _mentions_errno(node.body)
        ):
            blocks.append((node.body, function))
        if isinstance(node, ast.If) and _mentions_errno(node.test):
            blocks.append((node.body, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return blocks


def _target_names(node: ast.AST) -> set[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
        targets = [node.target]
    return {name for target in targets for name in _names(target)}


def _existence_facts(body: list[ast.stmt], function: str) -> list[int]:
    lines = []
    for statement in body:
        for node in ast.walk(statement):
            names = _target_names(node)
            if isinstance(node, ast.keyword) and node.arg is not None:
                names.add(node.arg)
            if isinstance(node, ast.Dict):
                names.update(
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                )
            built = isinstance(node, ast.Call) and _STATE_TYPE in _names(node.func)
            returned = isinstance(node, ast.Return) and _EXISTENCE in function.lower()
            if built or returned or any(_EXISTENCE in n.lower() for n in names):
                lines.append(getattr(node, "lineno", statement.lineno))
    return lines


def inferred_existence(root: Path) -> list[str]:
    """``<relative path>:<line>`` for each existence fact from a failed login."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for body, function in _auth_blocks(tree):
            found.extend(
                f"{path.relative_to(root)}:{line}"
                for line in _existence_facts(body, function)
            )
    return sorted(set(found))


def test_the_walk_reaches_the_modules_that_survey_and_catch() -> None:
    modules = set(PACKAGE.rglob("*.py"))
    assert len(modules) > 10, f"no Python source under {PACKAGE}"
    for module in ("adapters/database.py", "adapters/staging.py", "core/restore.py"):
        assert PACKAGE / module in modules, module
    database = ast.parse((PACKAGE / "adapters" / "database.py").read_text("utf-8"))
    assert _auth_blocks(database), "the adapter's AuthenticationFailedError handlers"


def test_no_existence_fact_is_derived_from_a_failed_login() -> None:
    assert inferred_existence(PACKAGE) == []


def test_planted_inferences_are_seen_wherever_and_however_written(
    tmp_path: Path,
) -> None:
    deep = tmp_path / "adapters" / "deep"
    deep.mkdir(parents=True)
    (deep / "a.py").write_text(
        "try:\n"
        "    login()\n"
        "except wpdb.AuthenticationFailedError:\n"
        "    database_exists = False\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def survey():\n"
        "    try:\n"
        "        login()\n"
        "    except (OSError, AuthenticationFailedError):\n"
        "        return DatabaseTargetState(False, 0, False, None, None, 0, 0)\n",
        encoding="utf-8",
    )
    (tmp_path / "c.py").write_text(
        "def state(exc):\n"
        "    if exc.args[0] == 1045:\n"
        "        return {'user_exists': False}\n"
        "    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "d.py").write_text(
        "def database_exists():\n"
        "    try:\n"
        "        connect()\n"
        "    except AuthenticationFailedError:\n"
        "        return False\n"
        "    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "f.py").write_text(
        "try:\n"
        "    connect()\n"
        "except wpdb.AccessDeniedError:\n"
        "    database_exists = False\n"
        "def g(exc):\n"
        "    if exc.errno == 1044:\n"
        "        return {'database_exists': False}\n"
        "    return None\n",
        encoding="utf-8",
    )
    assert inferred_existence(tmp_path) == [
        "adapters/deep/a.py:4",
        "b.py:5",
        "c.py:3",
        "d.py:5",
        "f.py:4",
        "f.py:7",
    ]


def test_mapping_a_failed_login_to_a_credentials_error_is_allowed(
    tmp_path: Path,
) -> None:
    """The adapter's real shape: the account is reported, existence is not."""
    (tmp_path / "e.py").write_text(
        "def survey_target():\n"
        "    try:\n"
        "        login()\n"
        "    except wpdb.AuthenticationFailedError as exc:\n"
        "        raise CredentialsError(source) from exc\n"
        "    database_exists = admin_survey().database_exists\n",
        encoding="utf-8",
    )
    assert inferred_existence(tmp_path) == []
