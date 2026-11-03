"""AG-23 — the config package is ``l3io-wp-config`` / ``l3io.wp.config`` everywhere.

The retired name ``wpconfigr`` appears nowhere under ``src/l3io/``. Scope: every
file under that tree, walked — not only ``*.py``, so a docstring, a packaged
data file or a type stub is covered too. Case-insensitive.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "src" / "l3io"
RETIRED = "wpconfigr"


def _offenders(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and RETIRED in path.read_bytes().decode("utf-8", errors="replace").lower()
    )


def test_the_scope_is_the_source_tree() -> None:
    files = [p for p in SOURCE.rglob("*") if p.is_file()]
    assert len(files) > 10, f"no source under {SOURCE}"


def test_the_retired_name_appears_nowhere() -> None:
    offenders = _offenders(SOURCE)
    assert not offenders, f"say l3io-wp-config / l3io.wp.config (AG-23): {offenders}"


def test_the_guard_fires_deep_in_any_kind_of_file(tmp_path: Path) -> None:
    (tmp_path / "wp" / "backup" / "deep").mkdir(parents=True)
    (tmp_path / "wp" / "backup" / "deep" / "m.py").write_text(
        '"""Uses WpConfigR."""\n', encoding="utf-8"
    )
    (tmp_path / "wp" / "backup" / "py.typed").write_text("wpconfigr\n")
    (tmp_path / "wp" / "backup" / "clean.py").write_text("x = 1\n")
    assert _offenders(tmp_path) == ["wp/backup/deep/m.py", "wp/backup/py.typed"]
