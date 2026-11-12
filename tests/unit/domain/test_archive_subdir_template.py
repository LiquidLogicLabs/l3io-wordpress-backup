"""Owner decision 86 — the ``archive_subdir`` template, checked and resolved.

A relative path under the Archive directory built from ``{site}``, ``{yyyy}``,
``{mm}`` and ``{dd}`` by stdlib ``str.format_map``. Each way a template can
be wrong is refused on its own, naming the setting; the date tokens are the
UTC date of the same instant the default Archive name stamps, so a backup
just before midnight in a zone west of UTC goes in the next day's folder,
as its name says.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath

import pytest

from l3io.wp.backup.domain.archive_subdir import (
    ARCHIVE_SUBDIR_TOKENS,
    ARCHIVE_TREE_DEPTH,
    check_archive_subdir,
    resolve_archive_subdir,
)
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.storage_target import default_archive_name


@pytest.mark.parametrize(
    "template",
    [
        "{site}/{yyyy}/{mm}",
        "{site}/{yyyy}",
        "{site}",
        "archives",
        "{yyyy}{mm}{dd}",
        "x-{site}.d/{yyyy}-{mm}",
        "/".join(["{site}"] * ARCHIVE_TREE_DEPTH),
    ],
)
def test_a_good_template_is_accepted_as_written(template: str) -> None:
    assert check_archive_subdir(template) == template


@pytest.mark.parametrize(
    ("template", "why"),
    [
        ("", "is empty"),
        ("{host}", "unknown token {host}"),
        ("{0}", "unknown token {0}"),
        ("{}", "unknown token {}"),
        ("{site.upper}", "unknown token {site.upper}"),
        ("{site[0]}", "unknown token {site[0]}"),
        ("{site:>10}", "a format spec or conversion"),
        ("{yyyy!r}", "a format spec or conversion"),
        ("{site", "unbalanced"),
        ("site}", "unbalanced"),
        ("a{{b", "a literal brace"),
        ("/srv/{site}", "absolute"),
        ("../{site}", "'..'"),
        ("{site}/../x", "'..'"),
        ("./{site}", "'.'"),
        ("a//b", "an empty segment"),
        ("{site}/", "an empty segment"),
        ("a b", "only A-Z"),
        ("a\\b", "only A-Z"),
        ("café", "only A-Z"),
        ("a\x00b", "only A-Z"),
        ("/".join(["a"] * (ARCHIVE_TREE_DEPTH + 1)), "levels"),
    ],
)
def test_each_bad_template_is_refused_naming_the_setting(
    template: str, why: str
) -> None:
    with pytest.raises(UsageError) as caught:
        check_archive_subdir(template)
    assert caught.value.arguments == ("archive_subdir",)
    assert why in str(caught.value)
    assert "--archive-subdir" in str(caught.value)


def test_the_tokens_are_the_owners_four() -> None:
    assert ARCHIVE_SUBDIR_TOKENS == ("site", "yyyy", "mm", "dd")


def test_the_date_is_utc_and_agrees_with_the_archive_name_at_a_day_boundary() -> None:
    # 23:30 on 31 Dec in New York (UTC-5) is 04:30 on 1 Jan in UTC.
    now = datetime(2026, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    path = resolve_archive_subdir("{site}/{yyyy}/{mm}/{dd}", site="shop", now=now)
    assert path == PurePosixPath("shop/2027/01/01")
    name = default_archive_name("shop", now)
    assert name == "wpbak-shop-20270101T043000Z.tar.gz"
    assert name.split("-")[2][:8] == "".join(path.parts[1:])


def test_site_is_the_archive_names_sanitised_site() -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    path = resolve_archive_subdir("{site}/{yyyy}", site="Example Shop", now=now)
    assert path == PurePosixPath("Example-Shop/2026")
    assert default_archive_name("Example Shop", now).startswith("wpbak-Example-Shop-")


@pytest.mark.parametrize("site", ["..", "."])
def test_a_site_that_would_climb_or_vanish_is_refused(site: str) -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    with pytest.raises(UsageError) as caught:
        resolve_archive_subdir("{site}/{yyyy}", site=site, now=now)
    assert "escape" in str(caught.value) or "'.'" in str(caught.value)


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(UsageError):
        resolve_archive_subdir("{yyyy}", site="s", now=datetime(2026, 1, 1))
