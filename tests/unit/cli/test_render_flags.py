"""BL-E003-039 — ``render_flags`` spells a refusal's flags as the operator types them.

For every ``FLAG_MAP`` key that expresses a Mode (scope from the table), the
rendered text, split and parsed by the real parser and read back through
``flag_combination``, is the same key: so no refusal names a flag the parser
rejects (``--mode database``) or a dimension without its dashes.
"""

from __future__ import annotations

import shlex

import pytest

from l3io.wp.backup.cli.parser import RESTORE_COMMAND, build_parser
from l3io.wp.backup.cli.restore import (
    NonEmptyTargetRefusal,
    flag_combination,
    render_flags,
)
from l3io.wp.backup.core.restore import NonEmptyTargetError
from l3io.wp.backup.core.restore import render_flags as domain_render_flags
from l3io.wp.backup.domain.flag_map import (
    FLAG_MAP,
    FlagCombination,
    FlagRejection,
    authorizing_combination,
)
from l3io.wp.backup.domain.restore import RestoreMode, restores_database, restores_files

KEYS = [k for k, e in FLAG_MAP.items() if not isinstance(e, FlagRejection)]


def _round_trip(text: str) -> FlagCombination:
    args = build_parser().parse_args(
        [RESTORE_COMMAND, "--wp-dir", "x", "--archive", "y", *shlex.split(text)]
    )
    return flag_combination(
        mode=args.mode,
        cleanfirst=args.cleanfirst,
        drop_database=args.drop_database,
        force=args.force,
    )


def test_the_scope_is_not_empty() -> None:
    assert len(KEYS) >= len(RestoreMode)


@pytest.mark.parametrize("key", KEYS, ids=[render_flags(k) for k in KEYS])
def test_the_rendered_flags_parse_back_to_the_same_key(key: FlagCombination) -> None:
    assert _round_trip(render_flags(key)) == key


def test_the_domain_spelling_does_not_parse() -> None:
    """The planted violation: core's spelling is what the CLI must not print."""
    database_key = next(k for k in KEYS if "database" in domain_render_flags(k))
    with pytest.raises(SystemExit):
        _round_trip(domain_render_flags(database_key))


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_refusal_names_the_cli_flags_never_the_domain_spelling(
    mode: RestoreMode,
) -> None:
    part = "files" if restores_files(mode) else "database"
    assert restores_files(mode) or restores_database(mode)
    original = NonEmptyTargetError(
        mode,
        parts=(part,),
        findings=(("'/srv/www'", "3 entries"),),
        authorizing=authorizing_combination(mode, part),
    )
    refusal = NonEmptyTargetRefusal(original)
    text = str(refusal)
    assert refusal.flags in text
    assert "--force" in refusal.flags or "--cleanfirst" in refusal.flags
    assert domain_render_flags(original.authorizing) not in text
    assert "--mode database" not in text
    assert f"(Restore Mode {mode.value})" in text
    assert refusal.exit_code is original.exit_code
