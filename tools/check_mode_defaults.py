#!/usr/bin/env python3
"""AD-5 — no function consuming a Restore Mode may declare a default for it.

This is the invariant that maps directly to the unconditional-``rmtree`` data
loss: ``restore()`` received ``restore_mode`` and called its sub-steps without
it, so both fell back to an ``ALLCLEAN`` default and the destruction became
unconditional. A default is what made the omission silent.

**What this check covers**

* any parameter of any ``def``/``async def`` whose annotation mentions a
  guarded type and which declares a default — positional-only, positional,
  and keyword-only alike;
* any annotated class attribute mentioning a guarded type with a value, which
  is a constructor-parameter default for a dataclass or an attrs class and so
  is the same defect wearing different syntax.

An annotation "mentions" a guarded type if the name appears anywhere in it, so
``RestoreMode``, ``domain.RestoreMode``, ``RestoreMode | None``,
``Optional[RestoreMode]`` and the string form ``"RestoreMode"`` are all caught.

**What this check does NOT cover, stated so the next reader keeps looking**

* an unannotated parameter that happens to receive a Restore Mode. Nothing
  static can see that; ``mypy --strict`` requiring full annotation on every
  public surface is the guard for it.
* a default supplied by a wrapper — ``functools.partial(f, mode=...)`` or a
  ``**kwargs`` shim — which reintroduces the defect without a syntactic
  default anywhere.
* a module-level constant used as a default at a call site.

**Scope.** The file set is derived by walking the package tree, so a new module
is covered the moment it exists. The guarded *type names* are derived too
(AD-10/SD-02-8, global rule 4), by AST from the restore and backup vocabulary
modules, :data:`VOCABULARY` (``domain/restore.py``, ``domain/backup_scope.py``):
every ``Enum`` subclass, every ``Literal`` alias, and every class whose
annotated fields mention a type already guarded (``OverrideSet``, which holds
``Override``s), to a fixed point. A closed choice added to either module is
guarded the moment it exists. A missing vocabulary module, or one that yields
no type, is a scope error rather than a pass over nothing. A closed choice
defined in another module is not seen.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Iterator
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "l3io" / "wp" / "backup"

#: The modules that define the restore and backup vocabulary: the source of
#: truth the guarded type names are derived from.
VOCABULARY = (
    PACKAGE_ROOT / "domain" / "restore.py",
    PACKAGE_ROOT / "domain" / "backup_scope.py",
)


def _is_enum(node: ast.ClassDef) -> bool:
    return any(
        ast.unparse(base).rsplit(".", 1)[-1].endswith(("Enum", "Flag"))
        for base in node.bases
    )


def _literal_alias(node: ast.stmt) -> str | None:
    """``X = Literal[...]`` / ``X: TypeAlias = Literal[...]`` / ``type X = ...``."""
    type_alias = getattr(ast, "TypeAlias", None)  # the `type` statement, 3.12+
    if type_alias is not None and isinstance(node, type_alias):
        value: ast.expr | None = node.value
        target: ast.expr = node.name
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        value, target = node.value, node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        value, target = node.value, node.target
    else:
        return None
    if not isinstance(target, ast.Name) or not isinstance(value, ast.Subscript):
        return None
    if ast.unparse(value.value).rsplit(".", 1)[-1] != "Literal":
        return None
    return target.id


def guarded_types(sources: tuple[Path, ...] = VOCABULARY) -> frozenset[str]:
    """Every closed choice the vocabulary modules define, derived by AST."""
    names: set[str] = set()
    classes: list[ast.ClassDef] = []
    for path in sources:
        if not path.is_file():
            msg = f"AD-5 scope error: vocabulary module {path} does not exist."
            raise SystemExit(msg)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if _is_enum(node):
                    names.add(node.name)
                else:
                    classes.append(node)
            alias = _literal_alias(node)
            if alias is not None:
                names.add(alias)
    grew = True
    while grew:
        grew = False
        for node in classes:
            if node.name in names:
                continue
            fields = (s for s in node.body if isinstance(s, ast.AnnAssign))
            if any(_mentions(s.annotation, frozenset(names)) for s in fields):
                names.add(node.name)
                grew = True
    if not names:
        msg = f"AD-5 scope error: no closed choice found in {sources}."
        raise SystemExit(msg)
    return frozenset(names)


def _mentions(annotation: ast.expr | None, guarded: frozenset[str]) -> str | None:
    """Return the guarded type named anywhere in ``annotation``, if any."""
    if annotation is None:
        return None
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id in guarded:
            return node.id
        if isinstance(node, ast.Attribute) and node.attr in guarded:
            return node.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            found = _mentions(inner, guarded)
            if found is not None:
                return found
    return None


#: The derived set over the real tree (read by tests; :func:`main` re-derives).
GUARDED_TYPES = guarded_types()


def _defaulted_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef, guarded_names: frozenset[str]
) -> Iterator[tuple[ast.arg, str]]:
    """Yield each parameter that both declares a default and names a guard."""
    args = node.args
    positional = args.posonlyargs + args.args
    # `defaults` aligns with the TAIL of the positional parameters.
    offset = len(positional) - len(args.defaults)
    for index, parameter in enumerate(positional):
        if index >= offset:
            guarded = _mentions(parameter.annotation, guarded_names)
            if guarded is not None:
                yield parameter, guarded
    for parameter, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is None:
            continue
        guarded = _mentions(parameter.annotation, guarded_names)
        if guarded is not None:
            yield parameter, guarded


def _check(
    path: Path, tree: ast.Module, guarded_names: frozenset[str]
) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for parameter, guarded in _defaulted_parameters(node, guarded_names):
                yield (
                    f"{path}:{parameter.lineno}: AD-5 defaulted mode: "
                    f"`{node.name}({parameter.arg}: ...{guarded}... = ...)` — "
                    f"a Restore Mode is resolved once and passed explicitly; "
                    f"a default is how it gets silently forgotten"
                )
        elif isinstance(node, ast.ClassDef):
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign) or statement.value is None:
                    continue
                guarded = _mentions(statement.annotation, guarded_names)
                if guarded is not None:
                    target = ast.unparse(statement.target)
                    yield (
                        f"{path}:{statement.lineno}: AD-5 defaulted mode: "
                        f"`{node.name}.{target}: ...{guarded}... = ...` — a class "
                        f"attribute default is a constructor-parameter default"
                    )


def main() -> int:
    if not PACKAGE_ROOT.is_dir():
        msg = (
            f"AD-5 scope error: {PACKAGE_ROOT} does not exist. The guard would "
            f"otherwise report success over an empty set."
        )
        raise SystemExit(msg)
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    if not files:
        msg = f"AD-5 scope error: no Python modules under {PACKAGE_ROOT}."
        raise SystemExit(msg)

    guarded_names = guarded_types(VOCABULARY)
    failures: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(_check(path, tree, guarded_names))

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for failure in failures:
        if in_actions:
            location, _, message = failure.partition(": ")
            file_name, _, line = location.rpartition(":")
            print(f"::error file={file_name},line={line}::{message}")
        print(failure, file=sys.stderr)

    if failures:
        print(
            f"\nAD-5 check FAILED: {len(failures)} defaulted mode parameter(s) "
            f"across {len(files)} module(s).",
            file=sys.stderr,
        )
        return 1
    print(
        f"AD-5 check passed over {len(files)} module(s), "
        f"{len(guarded_names)} guarded type(s): {', '.join(sorted(guarded_names))}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
