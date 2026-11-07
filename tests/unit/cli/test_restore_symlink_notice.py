"""RT-6 (BL-E004-054): a skipped symlink's notice quotes its Archive text."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.domain.archive_format import SkippedLink


def test_a_skipped_links_control_characters_never_reach_the_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = SkippedLink(path="a\x1b[2Jb", link_target="/etc\npasswd", reason="absolute")
    outcome = SimpleNamespace(
        skipped_links=(link,), resolved_run_id="run-5a", dry_run=False
    )
    restore_mod._print_outcome(  # type: ignore[arg-type]  # noqa: SLF001
        outcome, archive="a.tar", site_root="/srv", completed=False
    )
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert err.count("\n") == 1
    assert "'a\\x1b[2Jb' -> '/etc\\npasswd'" in err
