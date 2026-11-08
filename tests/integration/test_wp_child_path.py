"""BL-E007-013 — the ``wp`` child gets only the absolute ``PATH`` entries, and runs.

``adapters/wpcli.py::wp_cli_environment`` passes the child the ``PATH``
``wp`` was located over (``adapters/preflight.py::absolute_search_path``):
an empty entry, ``.`` and a relative directory are dropped. WP-CLI is a PHP
program that finds ``php`` on that ``PATH``, so this proves, with the real
WP-CLI 2.12.0 and PHP the CI runner installs, that a real relocation still
runs when the process ``PATH`` carries such entries — and that no child saw
one.

Reuses ``test_search_replace.py``'s installed WordPress fixture. Skips
without the CI database, ``wp`` or ``php``; a ``php`` without ``mysqli`` or a
failed core download skips locally and fails when ``CI`` is set.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from l3io.wp.backup.adapters import wpcli
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import WpCliSearchReplace
from l3io.wp.backup.domain.search_replace import UrlRelocation
from tests.integration.test_search_replace import (
    NEW,
    Site,
    _oracle,
    site,
    wordpress_core,
)

__all__ = ["site", "wordpress_core"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]


def test_wp_finds_php_with_only_the_absolute_path_entries(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = Path("relative-bin")
    absolute = [
        e for e in os.environ["PATH"].split(os.pathsep) if Path(e).is_absolute()
    ]
    assert absolute, "the runner's PATH has no absolute entry"
    sep = os.pathsep
    monkeypatch.setenv("PATH", sep.join(["", ".", str(relative), *absolute]))
    seen: list[str] = []

    def recording(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        seen.append(env["PATH"])
        return wpcli.run_child(argv, cwd=cwd, env=env)

    before = _oracle(site.database)
    assert before > 0, "the fixture did not seed the old URL"
    report = WpCliSearchReplace(
        site.wp,
        secrets=SecretRegistry(),
        run=recording,
    ).relocate_content(site.root, UrlRelocation(site_url=NEW, home_url=NEW))

    # php was found: every wp call succeeded and the URLs were rewritten.
    assert report.remaining == 0
    assert report.replacements == before
    assert _oracle(site.database) == 0
    assert seen, "no wp child was run"
    for path in seen:
        entries = path.split(os.pathsep)
        assert all(e and Path(e).is_absolute() for e in entries), entries
        assert entries == absolute
