"""E007-S01-002 (FR-29, AD-11) — the extension's config placement, on real files.

``WpCliRestoreExtension.resolve`` asks ``LocalSiteFiles.file_exists`` about
``<root>/wp-config.php``, ``<root>/../wp-config.php`` and
``<root>/../wp-settings.php`` under ``tmp_path``, and fills
``RestoreRelocation.config_file``/``lay_down_config``:

* ``place_config``'s existing-file rule agrees with ``locate_wp_config`` over
  every fact combination (``dataclasses.fields(ConfigFacts)``);
* a ``PARENT`` Archive restored into a fresh ``<dir>/public`` lays down at
  ``<dir>/wp-config.php``; a target config stands; a parent install, a foreign
  Manifest value and a shadowed parent file are refused before any effect;
* a v0 Archive (the committed fixture) passes ``None`` and lays nothing down;
* ``--wp-config`` is used as given, and a URL relocation whose stated config
  is outside both search places is ``USAGE``;
* BL-E003-056: every ``RestoreMode`` (from the enum) x a file at every
  ``ConfigLocation`` (from the enum) refuses with the file byte-identical or
  plans no lay-down over it, and a Mode restoring no files never lays down.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import itertools
import tarfile
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.database import locate_wp_config
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_extension import restore_extension
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import verify_archive_view
from l3io.wp.backup.cli.restore_request import RelocationFlags
from l3io.wp.backup.core.config_location import (
    ConfigFacts,
    existing_location,
)
from l3io.wp.backup.core.restore import RestoreRelocation
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ArchiveView,
    ConfigLocation,
    Integrity,
    ManifestPayload,
    MemberRole,
)
from l3io.wp.backup.domain.errors import (
    ConfigLocationConflictError,
    ExtractionRefusedError,
    InvalidSiteDescriptorError,
    RefusedError,
    UnsafeMemberPathError,
    UnsupportedConfigLocationError,
    UsageError,
)
from l3io.wp.backup.domain.restore import RestoreMode, restores_files
from l3io.wp.backup.domain.storage_target import StorageTarget
from tests.restore_builders import SITE, member, view

V0_FIXTURE = (
    Path(__file__).parents[2] / "fixtures/archives/v0/released-0.2.21-all.tar.gz"
)
#: The fact each ``ConfigFacts`` field names, as a file under ``<dir>``.
FACT_FILE = {
    "at_site_root": "public/wp-config.php",
    "at_parent": "wp-config.php",
    "parent_has_wp_settings": "wp-settings.php",
}
CONFIG_BODY = b"<?php\n// e007-s01-002 existing config, never rewritten here\n"


def test_fact_file_covers_every_fact() -> None:
    """Attacks the scope: a ``ConfigFacts`` field with no file here fails."""
    assert set(FACT_FILE) == {f.name for f in dataclasses.fields(ConfigFacts)}


def parent_view(config_path: str = ConfigLocation.PARENT.value) -> ArchiveView:
    """A v1 view whose config is a ``config``-role member (no in-tree copy)."""
    base = view(config=False)
    return dataclasses.replace(
        base,
        members=(*base.members, member("config/wp-config.php", MemberRole.CONFIG)),
        config_path=config_path,
    )


def _root(tmp_path: Path, present: dict[str, bool]) -> Path:
    base = tmp_path / "b"
    root = base / "public"
    root.mkdir(parents=True)
    for name, there in present.items():
        if there:
            (base / FACT_FILE[name]).write_bytes(CONFIG_BODY)
    return root.resolve()


def _resolve(
    root: Path,
    the_view: ArchiveView,
    mode: RestoreMode = RestoreMode.ALL,
    **flags: object,
) -> RestoreRelocation:
    request = dataclasses.replace(RelocationFlags.none(), **flags)  # type: ignore[arg-type]
    return restore_extension(request, secrets=SecretRegistry()).resolve(
        site_root=root,
        site=dataclasses.replace(SITE, site_root=PurePosixPath(root)),
        view=the_view,
        files=LocalSiteFiles(),
        mode=mode,
    )


def _snapshot(base: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()
    }


# --------------------------------------------------------------------------
# place_config and locate_wp_config agree on which existing file wins
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        dict(zip(FACT_FILE, combo, strict=True))
        for combo in itertools.product((False, True), repeat=len(FACT_FILE))
    ],
    ids=lambda v: "-".join(k for k, there in v.items() if there) or "none",
)
def test_place_config_and_locate_wp_config_agree(
    tmp_path: Path, values: dict[str, bool]
) -> None:
    root = _root(tmp_path, values)
    try:
        located = locate_wp_config(root, stated=None).location
    except InvalidSiteDescriptorError:
        located = None
    assert existing_location(ConfigFacts(**values)) is located


# --------------------------------------------------------------------------
# The PARENT lay-down, and what stands or is refused instead
# --------------------------------------------------------------------------


def test_a_parent_config_is_laid_down_one_level_up(tmp_path: Path) -> None:
    root = _root(tmp_path, {})
    with capture_logs() as logs:
        relocation = _resolve(root, parent_view())
    assert relocation.config_file == PurePosixPath(root.parent / "wp-config.php")
    assert relocation.lay_down_config is True
    assert ".." not in relocation.config_file.parts
    located = [e for e in logs if e["event"] == "config_located"]
    assert len(located) == 1
    assert located[0] | {"log_level": "info"} == {
        "event": "config_located",
        "log_level": "info",
        "config_path": str(root.parent / "wp-config.php"),
        "location": ConfigLocation.PARENT.value,
        "action": "lay-down",
        "source": "manifest",
        "manifest_config_path": ConfigLocation.PARENT.value,
    }
    # A lay-down alone is no relocation: nothing is logged as one.
    assert not [e for e in logs if e["event"] == "relocation_planned"]


@pytest.mark.parametrize("fact", ["at_site_root", "at_parent"])
def test_a_target_config_stands(tmp_path: Path, fact: str) -> None:
    root = _root(tmp_path, {fact: True})
    before = _snapshot(root.parent)
    relocation = _resolve(root, parent_view())
    assert relocation.config_file is None
    assert relocation.lay_down_config is False
    assert _snapshot(root.parent) == before


def test_a_parent_install_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path, {"parent_has_wp_settings": True})
    with pytest.raises(ConfigLocationConflictError) as caught:
        _resolve(root, parent_view())
    assert caught.value.path == PurePosixPath(root.parent)
    assert "wp-settings.php" in str(caught.value)
    assert not (root.parent / "wp-config.php").exists()


@pytest.mark.parametrize("value", ["../../x", "/etc/passwd", "wp-config.php/../x"])
def test_a_foreign_manifest_value_is_refused(tmp_path: Path, value: str) -> None:
    root = _root(tmp_path, {})
    with pytest.raises(UnsupportedConfigLocationError) as caught:
        _resolve(root, parent_view(value))
    assert caught.value.value == value
    assert _snapshot(tmp_path) == {}


def test_a_v0_archive_lays_its_config_at_the_default(tmp_path: Path) -> None:
    v0, _ = verify_archive_view(V0_FIXTURE)
    assert v0 is not None
    assert v0.format_version is None  # v0: no Manifest
    # Gate E004 AG-11: a v0 view names a member, and has no config role.
    assert v0.config_path == "wp-root/wp-config.php"
    assert not any(m.role is MemberRole.CONFIG for m in v0.members)
    root = _root(tmp_path, {})
    with capture_logs() as logs:
        relocation = _resolve(root, v0)
    # No PARENT invented: the tree's wp-config.php goes to the Site Root.
    assert relocation.config_file == PurePosixPath(root / "wp-config.php")
    assert relocation.lay_down_config is True
    (record,) = [e for e in logs if e["event"] == "config_located"]
    assert record["manifest_config_path"] is None
    assert record["source"] == "v0-default"
    assert record["action"] == "lay-down"
    # A Mode that restores no files lays nothing down.
    database_only = _resolve(root, v0, RestoreMode.DATABASE)
    assert database_only.lay_down_config is False
    assert record["config_path"] == str(root / "wp-config.php")


# --------------------------------------------------------------------------
# --wp-config
# --------------------------------------------------------------------------


def test_a_stated_config_is_used_as_given(tmp_path: Path) -> None:
    root = _root(tmp_path, {})
    stated = tmp_path / "elsewhere" / "wp-config.php"
    stated.parent.mkdir()
    stated.write_bytes(CONFIG_BODY)
    with capture_logs() as logs:
        relocation = _resolve(root, parent_view(), wp_config=stated)
    assert relocation.config_file is None
    assert relocation.lay_down_config is False
    (record,) = [e for e in logs if e["event"] == "config_located"]
    assert record["source"] == "stated"
    assert record["location"] is None


def test_a_url_relocation_needs_a_config_wordpress_reads(tmp_path: Path) -> None:
    root = _root(tmp_path, {})
    stated = tmp_path / "elsewhere" / "wp-config.php"
    stated.parent.mkdir()
    stated.write_bytes(CONFIG_BODY)
    with pytest.raises(UsageError) as caught:
        _resolve(
            root,
            parent_view(),
            wp_config=stated,
            new_site_url="https://new.e007.example",
        )
    assert not isinstance(caught.value, RefusedError)
    assert str(root / "wp-config.php") in str(caught.value)
    assert str(root.parent / "wp-config.php") in str(caught.value)


@pytest.mark.parametrize("fact", ["at_site_root", "at_parent"])
def test_a_url_relocation_accepts_a_stated_config_at_a_search_place(
    tmp_path: Path, fact: str
) -> None:
    root = _root(tmp_path, {fact: True})
    stated = root.parent / FACT_FILE[fact]
    relocation = _resolve(
        root, parent_view(), wp_config=stated, new_site_url="https://new.e007.example"
    )
    assert relocation.lay_down_config is False


# --------------------------------------------------------------------------
# BL-E003-056: a lay-down never writes over a file outside FR-7 and the snapshot
# --------------------------------------------------------------------------


#: Every non-empty set of occupied ``ConfigLocation``s, from the enum.
OCCUPIED = [
    frozenset(combo)
    for size in range(1, len(ConfigLocation) + 1)
    for combo in itertools.combinations(ConfigLocation, size)
]
_FACT_OF = {
    ConfigLocation.SITE_ROOT: "at_site_root",
    ConfigLocation.PARENT: "at_parent",
}


def test_fact_of_covers_every_location() -> None:
    """Attacks the scope: a ``ConfigLocation`` with no fact here fails."""
    assert set(_FACT_OF) == set(ConfigLocation)


@pytest.mark.parametrize("mode", list(RestoreMode))
@pytest.mark.parametrize(
    "occupied", OCCUPIED, ids=lambda o: "+".join(sorted(m.name for m in o))
)
@pytest.mark.parametrize("wanted", list(ConfigLocation))
def test_a_lay_down_never_writes_over_a_file(
    tmp_path: Path,
    mode: RestoreMode,
    occupied: frozenset[ConfigLocation],
    wanted: ConfigLocation,
) -> None:
    root = _root(tmp_path, {_FACT_OF[location]: True for location in occupied})
    in_parent = root.parent / "wp-config.php"
    # Refused only where the Manifest's place holds a file WordPress does not
    # read (a PARENT file shadowed by a Site Root one); else the file there is
    # the one the root located at step 3, inside FR-7's and the snapshot's
    # scope, and nothing is laid down over it.
    if wanted is ConfigLocation.PARENT and len(occupied) == len(ConfigLocation):
        with pytest.raises(ConfigLocationConflictError) as refused:
            _resolve(root, parent_view(wanted.value), mode)
        assert refused.value.path == PurePosixPath(in_parent)
        assert str(in_parent) in str(refused.value)
    else:
        relocation = _resolve(root, parent_view(wanted.value), mode)
        assert relocation.lay_down_config is False
        assert relocation.config_file is None
    # resolve writes nothing either way; that the refusal comes before any
    # effect is driven through the root in tests/unit/cli/
    # test_restore_config_parent.py::test_a_shadowed_parent_file_is_refused_...


@pytest.mark.parametrize("mode", list(RestoreMode))
@pytest.mark.parametrize("wanted", list(ConfigLocation))
def test_only_a_mode_that_restores_files_lays_down(
    tmp_path: Path, mode: RestoreMode, wanted: ConfigLocation
) -> None:
    root = _root(tmp_path, {})
    relocation = _resolve(root, parent_view(wanted.value), mode)
    assert relocation.lay_down_config is restores_files(mode)


# --------------------------------------------------------------------------
# AD-11: the lay-down's extraction refuses a member escaping the parent
# --------------------------------------------------------------------------


def _crafted(tmp_path: Path, name: str) -> tuple[StorageTarget, ArchiveView]:
    """A v1 Archive whose one ``config``-role member is named ``name``."""
    body = b"<?php // crafted\n"
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w:gz") as inner:
        info = tarfile.TarInfo(name)
        info.size = len(body)
        inner.addfile(info, io.BytesIO(body))
    payload = plain.getvalue()
    path = tmp_path / "crafted.tar.gz"
    with tarfile.open(path, mode="w") as outer:
        for member_name, data in ((MANIFEST_MEMBER, b"{}"), (PAYLOAD_MEMBER, payload)):
            entry = tarfile.TarInfo(member_name)
            entry.size = len(data)
            outer.addfile(entry, io.BytesIO(data))
    crafted = dataclasses.replace(
        parent_view(),
        members=(member(name, MemberRole.CONFIG, len(body)),),
        dump_present=False,
        integrity=Integrity.VERIFIED,
        payload=ManifestPayload(
            name=PAYLOAD_MEMBER,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )
    return StorageTarget(scheme="file", path=PurePosixPath(path)), crafted


def test_a_config_member_escaping_the_parent_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path, {})
    relocation = _resolve(root, parent_view())
    assert relocation.config_file is not None
    source, crafted = _crafted(tmp_path, "config/../../escape.php")
    before = _snapshot(tmp_path / "b")
    with pytest.raises((ExtractionRefusedError, UnsafeMemberPathError)):
        LocalSiteFiles().extract(
            source,
            Path(relocation.config_file).parent,
            crafted,
            roles=frozenset({MemberRole.CONFIG}),
        )
    assert _snapshot(tmp_path / "b") == before
    assert not list(tmp_path.rglob("escape.php"))


# --------------------------------------------------------------------------
# Review fixes: the file WordPress loads, odd entries, the Manifest always read
# --------------------------------------------------------------------------

URL = {"new_site_url": "https://new.e007.example"}


@pytest.mark.parametrize(
    ("present", "stated_fact"),
    [
        # WordPress reads the Site Root's file first: the parent one is ignored.
        ({"at_site_root": True, "at_parent": True}, "at_parent"),
        # A parent holding wp-settings.php is another install: never read.
        ({"at_parent": True, "parent_has_wp_settings": True}, "at_parent"),
    ],
    ids=["shadowed-by-root", "parent-is-an-install"],
)
def test_a_url_relocation_refuses_a_config_wordpress_does_not_load(
    tmp_path: Path, present: dict[str, bool], stated_fact: str
) -> None:
    root = _root(tmp_path, present)
    with pytest.raises(UsageError) as caught:
        _resolve(root, view(), wp_config=root.parent / FACT_FILE[stated_fact], **URL)
    assert not isinstance(caught.value, RefusedError)
    assert "not the file WordPress loads" in str(caught.value)


def test_a_url_relocation_accepts_the_shadowing_root_config(tmp_path: Path) -> None:
    root = _root(tmp_path, {"at_site_root": True, "at_parent": True})
    stated = root.parent / FACT_FILE["at_site_root"]
    assert _resolve(root, view(), wp_config=stated, **URL).lay_down_config is False


def test_a_foreign_manifest_value_is_refused_with_a_stated_config(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, {"at_site_root": True})
    with pytest.raises(UnsupportedConfigLocationError):
        _resolve(
            root,
            parent_view("../../x"),
            wp_config=root.parent / FACT_FILE["at_site_root"],
        )


def _plant(path: Path, kind: str) -> None:
    if kind == "directory":
        path.mkdir()
    elif kind == "dangling-symlink":
        path.symlink_to(path.parent / "missing-target.php")
    elif kind == "symlink-to-file":
        target = path.parent / f"real-{path.name}"
        target.write_bytes(CONFIG_BODY)
        path.symlink_to(target)
    else:
        path.write_bytes(CONFIG_BODY)


KINDS = ["file", "directory", "dangling-symlink", "symlink-to-file"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("where", list(ConfigLocation))
def test_an_entry_of_any_kind_is_used_or_refused_never_laid_over(
    tmp_path: Path, kind: str, where: ConfigLocation
) -> None:
    """``locate_wp_config`` and ``resolve`` agree; nothing is laid over an entry."""
    root = _root(tmp_path, {})
    entry = (
        root / "wp-config.php"
        if where is ConfigLocation.SITE_ROOT
        else (root.parent / "wp-config.php")
    )
    _plant(entry, kind)
    try:
        located = locate_wp_config(root, stated=None).location
    except InvalidSiteDescriptorError:
        located = None
    if located is not None:
        assert located is where
        assert _resolve(root, parent_view(where.value)).lay_down_config is False
        return
    with pytest.raises(ConfigLocationConflictError) as caught:
        _resolve(root, parent_view(where.value))
    assert caught.value.path == PurePosixPath(entry)
