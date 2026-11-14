"""Owner decision 86 — ``archive_subdir``: an Archive subfolder template.

A relative path whose segments may hold the tokens ``{site}``, ``{yyyy}``,
``{mm}`` and ``{dd}`` (``{site}/{yyyy}/{mm}``). It is parsed by stdlib
:class:`string.Formatter` and filled by ``str.format_map`` over a mapping of
exactly those four names; nothing here substitutes text by hand.

**Checked when the setting is read** (:func:`check_archive_subdir`, the
parser's ``type``, so the flag, the variable and the config key are checked
alike): braces only as whole tokens (an unknown name, a positional ``{}``, an
attribute or index, a format spec, a conversion, an escaped ``{{`` and an
unbalanced brace are refused); otherwise only ``A-Z a-z 0-9 . _ - /``;
relative, with no empty, ``.`` or ``..`` segment; at most
:data:`ARCHIVE_TREE_DEPTH` levels, the depth ``restore --archive latest`` and
``prune`` search, so every Archive a backup writes is one they find.

**Flat, said outright:** the setting's value :data:`FLAT_ARCHIVE_SUBDIR`
(``none``) means no subfolder, so a flag or a variable can turn off a
template a config file or the environment sets, for one Run. It names no
folder (:func:`check_archive_subdir` refuses it as a template); any other
capitalisation (``None``, ``NONE``) is refused as ambiguous rather than made
a folder. A segment named ``none`` in a longer template is a folder.

**Resolved per backup** (:func:`resolve_archive_subdir`): ``{site}`` is the
default Archive name's ``<site>`` (``storage_target.archive_site_name``), and
the date tokens are the UTC date of the instant the name stamps, so the
folder and the name always agree. A ``{site}`` that fills a whole segment
with ``.`` or ``..`` would leave the Archive directory or vanish, and is
refused. Pure: the caller checks the folders on disk.
"""

from __future__ import annotations

import re
import string
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.storage_target import archive_site_name

__all__ = [
    "ARCHIVE_SUBDIR_TOKENS",
    "ARCHIVE_TREE_DEPTH",
    "FLAT_ARCHIVE_SUBDIR",
    "archive_subdir_problem",
    "archive_subdir_reason",
    "check_archive_subdir",
    "resolve_archive_subdir",
]

#: The tokens a template may use (owner decision 86).
ARCHIVE_SUBDIR_TOKENS: Final = ("site", "yyyy", "mm", "dd")
#: How many folder levels below the Archive directory a template may have,
#: and how deep ``latest`` and ``prune`` search: ``{site}/{yyyy}/{mm}/{dd}``
#: is four, and two more leave room for a fixed prefix or a host level.
ARCHIVE_TREE_DEPTH: Final = 6
#: The setting's value for no subfolder: the Archive directory, flat.
FLAT_ARCHIVE_SUBDIR: Final = "none"
_ARGUMENT: Final = ("archive_subdir",)
_FLAG: Final = "--archive-subdir"
#: A validity check of the whole template, not a substitution.
_ALLOWED: Final = re.compile(r"[A-Za-z0-9._\-{}/]+")


def _explained(why: str) -> str:
    tokens = ", ".join(f"{{{t}}}" for t in ARCHIVE_SUBDIR_TOKENS)
    return (
        f"{why} (a relative path under the archive directory, using {tokens}; "
        f"or {FLAT_ARCHIVE_SUBDIR!r} for none)"
    )


def _why(template: str) -> str | None:
    """The first rule ``template`` breaks, or ``None``."""
    if template == "":
        return "is empty"
    if template == FLAT_ARCHIVE_SUBDIR:
        return "is the setting's word for no subfolder and names no folder"
    if template.lower() == FLAT_ARCHIVE_SUBDIR:
        return (
            f"is ambiguous: write {FLAT_ARCHIVE_SUBDIR!r} for no subfolder, or "
            "a template"
        )
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return "has an unbalanced brace"
    for literal, name, spec, conversion in parsed:
        if "{" in literal or "}" in literal:
            return "has a literal brace ('{{' or '}}')"
        if name is None:
            continue
        if name not in ARCHIVE_SUBDIR_TOKENS:
            return f"has an unknown token {{{name}}}"
        if spec or conversion is not None:
            return "has a format spec or conversion in a token"
    if not _ALLOWED.fullmatch(template):
        return "may hold only A-Z, a-z, 0-9, '.', '_', '-', '/'"
    if template.startswith("/"):
        return "is absolute"
    segments = template.split("/")
    for segment in segments:
        if segment == "":
            return "has an empty segment"
        if segment in {".", ".."}:
            return f"has a {segment!r} segment"
    if len(segments) > ARCHIVE_TREE_DEPTH:
        return f"has {len(segments)} levels; at most {ARCHIVE_TREE_DEPTH} are searched"
    return None


def archive_subdir_reason(template: str) -> str | None:
    """Why ``template`` is not a valid ``archive_subdir`` template, without
    naming the setting or the value (the caller names where it came from);
    ``None`` when it is valid."""
    why = _why(template)
    return None if why is None else _explained(why)


def archive_subdir_problem(template: str) -> str | None:
    """Why ``template`` is not a valid ``archive_subdir``, as the flag's
    refusal says it; ``None`` when it is valid."""
    reason = archive_subdir_reason(template)
    return None if reason is None else f"{_FLAG} {template!r}: {reason}"


def check_archive_subdir(template: str) -> str:
    """``template`` when it is a valid ``archive_subdir``; else :class:`UsageError`."""
    problem = archive_subdir_problem(template)
    if problem is not None:
        raise UsageError(problem, arguments=_ARGUMENT)
    return template


def resolve_archive_subdir(template: str, *, site: str, now: datetime) -> PurePosixPath:
    """``template`` filled for one backup: a relative path of folders.

    ``site`` goes through :func:`archive_site_name`, as the Archive name's
    does; ``now`` must be timezone-aware and is the instant the name stamps.
    """
    check_archive_subdir(template)
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "resolve_archive_subdir needs a timezone-aware datetime"
        raise UsageError(msg, arguments=("now",))
    utc = now.astimezone(UTC)
    values = {
        "site": archive_site_name(site),
        "yyyy": f"{utc.year:04d}",
        "mm": f"{utc.month:02d}",
        "dd": f"{utc.day:02d}",
    }
    filled = template.format_map(values)
    for segment in filled.split("/"):
        if segment in {".", ".."}:
            msg = (
                f"{_FLAG} {template!r} with site name {values['site']!r} would "
                "escape the archive directory (a segment is "
                f"{segment!r}); set --site-name"
            )
            raise UsageError(msg, arguments=("site_name",))
    return PurePosixPath(filled)
