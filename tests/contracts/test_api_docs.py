"""FR-24 — ``docs/api.md`` and the public surface agree, in both directions.

Every public name (``test_public_surface.public_names``: the two ``__all__``
lists, at run time) has exactly one heading of the form ``### `name` ``, and
every such heading names a public name. Beyond the headings, each section is
checked for what can be checked mechanically:

* every parameter of a public callable or class, every member of a public
  enum and every public member of a public protocol is named in backticks;
* a public name defined under ``cli/`` that is not a ``BackupError`` (the
  exception set is governed by E006-S01-002's walk) carries the line
  :data:`CLI_LINE` (arch gate C1; derived from ``__module__``, listed nowhere);
* every ``L3IO_WP_BACKUP_*`` variable the source names is on the page;
* the preface names every internal layer, derived from the import-linter
  ``layers`` contract.

**Not checked:** whether a parameter's *description*, type or default is
right, whether a callable's section states its return and the exceptions
it raises, and whether the preface's prose is true. Those are reviewed.
"""

from __future__ import annotations

import ast
import enum
import inspect
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from l3io.wp.backup.domain.errors import BackupError
from tests.contracts.test_public_surface import internal_layers, public_names
from tests.exception_scope import PACKAGE, PACKAGE_DIR, PROJECT_ROOT

API_DOC = PROJECT_ROOT / "docs" / "api.md"
CLI_LINE = "Defined in the CLI layer; import it from l3io.wp.backup.api."
HEADING = re.compile(r"^### `(?P<name>[^`\s]+)`$")
SECTION_BREAK = re.compile(r"^#{1,3} ")
VARIABLE = re.compile(r"L3IO_WP_BACKUP_[A-Z0-9_]*[A-Z0-9]")


def sections(text: str) -> dict[str, str]:
    """Each ``### `name` `` heading's body, up to the next heading of level 1-3.

    A ``### `` line that is not in that form is an error rather than skipped,
    so a heading cannot fall out of the check by being misspelt.
    """
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and SECTION_BREAK.match(line):
            current = None
            if line.startswith("### "):
                match = HEADING.match(line)
                assert match, f"docs/api.md:{number}: malformed heading {line!r}"
                name = match["name"]
                assert name not in found, (
                    f"docs/api.md:{number}: {name} documented twice"
                )
                current = found[name] = []
                continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in found.items()}


def drift(documented: Iterable[str], public: Iterable[str]) -> list[str]:
    documented, public = set(documented), set(public)
    return [
        *(
            f"{n}: public, but docs/api.md has no heading for it"
            for n in sorted(public - documented)
        ),
        *(
            f"{n}: docs/api.md has a heading, but it is not public"
            for n in sorted(documented - public)
        ),
    ]


def needs_cli_line(obj: object) -> bool:
    module = getattr(obj, "__module__", None) or ""
    in_cli = module == f"{PACKAGE}.cli" or module.startswith(f"{PACKAGE}.cli.")
    return in_cli and not (isinstance(obj, type) and issubclass(obj, BackupError))


def required_mentions(obj: object) -> list[str]:
    """The names a section must mention for ``obj``: params, members, methods."""
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        return [member.name for member in obj]
    if getattr(obj, "_is_protocol", False):
        return [name for name in vars(obj) if not name.startswith("_")]
    if not callable(obj):
        return []
    try:
        signature = inspect.signature(obj)
    except ValueError:  # a class with no __init__ of its own: BackupError
        return []
    return [name for name in signature.parameters if not name.startswith("_")]


def section_defects(docs: Mapping[str, str], public: Mapping[str, object]) -> list[str]:
    defects = []
    for name, obj in public.items():
        body = docs.get(name)
        if body is None:
            continue  # reported by drift()
        missing = [m for m in required_mentions(obj) if f"`{m}`" not in body]
        if missing:
            defects.append(f"{name}: section does not name {missing}")
        if needs_cli_line(obj) and CLI_LINE not in body:
            defects.append(
                f"{name}: defined under cli/ and its section lacks {CLI_LINE!r}"
            )
    return defects


def source_variables(package_dir: Path) -> set[str]:
    """Every string constant in the source that *is* an ``L3IO_WP_BACKUP_*`` name."""
    return {
        node.value
        for path in package_dir.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text("utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and VARIABLE.fullmatch(node.value)
    }


@pytest.fixture(scope="module")
def text() -> str:
    return API_DOC.read_text(encoding="utf-8")


def test_every_public_name_has_a_heading_and_every_heading_a_name(text: str) -> None:
    assert drift(sections(text), public_names()) == []


def test_drift_is_reported_in_both_directions(text: str) -> None:
    documented = sections(text)
    public = public_names()
    assert drift(documented, [*public, "PlantedName"]) == [
        "PlantedName: public, but docs/api.md has no heading for it"
    ]
    assert drift([*documented, "planted_heading"], public) == [
        "planted_heading: docs/api.md has a heading, but it is not public"
    ]


def test_a_malformed_or_repeated_heading_is_an_error() -> None:
    with pytest.raises(AssertionError, match="malformed heading"):
        sections("### backup\n")
    with pytest.raises(AssertionError, match="documented twice"):
        sections("### `backup`\n\n### `backup`\n")
    # A heading-like line inside a code fence is content, not a heading.
    assert sections("### `a`\n```\n### not a heading\n```\n") == {
        "a": "```\n### not a heading\n```"
    }


def test_every_section_documents_what_its_name_takes(text: str) -> None:
    assert section_defects(sections(text), public_names()) == []


def test_the_cli_layer_note_is_derived_and_enforced(text: str) -> None:
    public = public_names()
    derived = {name for name, obj in public.items() if needs_cli_line(obj)}
    # Scope: the derivation reaches the re-exports E006-S01-001 recorded.
    # RunContext left the surface (owner decision 2026-11-07, BL-E008-028).
    assert {
        "BackupResult",
        "RestoreOutcome",
        "RelocationFlags",
    } <= derived
    assert "NonEmptyTargetRefusal" not in derived  # a BackupError: governed elsewhere

    planted = sections(text)
    planted["BackupResult"] = planted["BackupResult"].replace(CLI_LINE, "")
    assert section_defects(planted, {"BackupResult": public["BackupResult"]}) == [
        f"BackupResult: defined under cli/ and its section lacks {CLI_LINE!r}"
    ]


def test_a_missing_parameter_is_caught(text: str) -> None:
    public = public_names()
    planted = sections(text)
    planted["backup"] = planted["backup"].replace("`dry_run`", "dry_run")
    planted["BackupScope"] = planted["BackupScope"].replace("`FILES`", "")
    defects = section_defects(
        planted, {n: public[n] for n in ("backup", "BackupScope")}
    )
    assert defects == [
        "backup: section does not name ['dry_run']",
        "BackupScope: section does not name ['FILES']",
    ]


def test_every_environment_variable_the_source_names_is_listed(text: str) -> None:
    variables = source_variables(PACKAGE_DIR)
    assert "L3IO_WP_BACKUP_PASSPHRASE" in variables  # scope: the walk reached it
    assert sorted(v for v in variables if f"`{v}`" not in text) == []


def test_the_preface_names_every_internal_layer(text: str) -> None:
    preface = text.split("\n## ", 2)[1]
    missing = [
        layer
        for layer in sorted(internal_layers())
        if f"`.{layer}`" not in preface and f"`{PACKAGE}.{layer}`" not in preface
    ]
    assert missing == []


KEYWORD = re.compile(r"(?<![\w.])`([a-z_][a-z0-9_]*)=")
#: A line that says a parameter is gone may name it (the guard's one exemption).
RETIRED_WORDS = ("removed", "is gone")


def live_parameters(public: Mapping[str, object]) -> set[str]:
    """Every parameter name of every public callable, derived at run time."""
    return {p for obj in public.values() for p in required_mentions(obj)}


def stale_keywords(text: str, public: Mapping[str, object]) -> list[str]:
    """E008 closure ED-3: a backticked ``name=`` in prose that is no live parameter."""
    live = live_parameters(public)
    return [
        f"docs/api.md:{number}: `{match[1]}=` is not a parameter of any public name"
        for number, line in enumerate(text.splitlines(), start=1)
        if not any(word in line for word in RETIRED_WORDS)
        for match in KEYWORD.finditer(line)
        if match[1] not in live
    ]


def test_prose_names_only_live_keyword_arguments(text: str) -> None:
    assert stale_keywords(text, public_names()) == []


def test_a_removed_keyword_left_in_prose_is_caught(text: str) -> None:
    planted = text + "\nA call that joined a Run through `context=` writes no record.\n"
    defects = stale_keywords(planted, public_names())
    assert len(defects) == 1
    assert "`context=`" in defects[0]
