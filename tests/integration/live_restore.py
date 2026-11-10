"""A live restore over the CI database, for E003-S02-005's integration tests.

Not a test module. :func:`restore` drives ``execute_restore`` with the real
``check_live``/``load_live`` slots over the real adapters — ``WpDatabase``
as ``live_account``'s throwaway account (also the administrator),
``WpConfigWriter``, ``LocalSiteFiles``, ``TarArchiveProbe`` and
``FileRunJournal`` — not the library entry point (Epic 6) and not
``drive_restore`` (E003-S02-006). The Safety Snapshot is skipped unless
``RestoreSpec.snapshot`` asks for one: then the live database is dumped as
the WordPress account into a real Archive (:func:`live_snapshot`), which the
automatic rollback (owner decision 50) reloads.

:class:`LiveSite` owns a throwaway live database whose name is unique to the
test, seeded WordPress-shaped; its teardown drops that database and every
database whose name starts with it (``--new-db-name`` databases the tests
create), through CI's own connection only.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from multiprocessing.connection import Connection as PipeEnd
from pathlib import Path, PurePosixPath

import pytest

from l3io.wp.backup.adapters.config_writer import WpConfigWriter
from l3io.wp.backup.adapters.database import (
    LocatedConfig,
    WpDatabase,
    admin_source,
    locate_wp_config,
    site_descriptor_from_archive_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.database_load import (
    check_live,
    load_live,
    roll_back_relocation,
)
from l3io.wp.backup.adapters.journal import FileRunJournal, journal_directory
from l3io.wp.backup.adapters.noop import NoopTracer
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_runner import execute_restore, snapshot_result
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    collect_site_root,
    file_member,
    verify_archive_view,
    write_archive,
)
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    ExecutionReport,
    RestoreInputs,
    RestoreRelocation,
    SnapshotResult,
    TargetState,
    build_restore_plan,
    database_rollback,
    load_database_name,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.domain.archive_format import (
    DUMP_RELATIVE,
    FORMAT_VERSION,
    PAYLOAD_MEMBER,
    Manifest,
    ManifestMember,
    ManifestPayload,
    MemberRole,
    config_member,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING
from l3io.wp.backup.domain.journal import JOURNALED_CONSTANTS, JournalHeader
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.config import ConfigCommand
from l3io.wp.backup.ports.database import DatabaseCommand
from l3io.wp.backup.ports.journal import RunJournalCommand
from l3io.wp.backup.ports.restore import RestorePorts, restore_ports
from l3io.wp.backup.ports.site_files import SiteFilesCommand
from tests.conftest import CredentialSentinel
from tests.restore_harness import Unused

pymysql = pytest.importorskip("pymysql")

from pymysql.connections import Connection  # noqa: E402

PREFIX = "wp_"
#: A minimal WordPress-shaped Dump: ``wp_options`` holding ``db_version``.
WP_DUMP = (
    b"CREATE TABLE `wp_options` (\n"
    b"  `option_id` bigint unsigned NOT NULL AUTO_INCREMENT,\n"
    b"  `option_name` varchar(191) NOT NULL DEFAULT '',\n"
    b"  `option_value` longtext NOT NULL,\n"
    b"  `autoload` varchar(20) NOT NULL DEFAULT 'yes',\n"
    b"  PRIMARY KEY (`option_id`)\n"
    b");\n"
    b"INSERT INTO `wp_options` VALUES (1,'db_version','57155','yes');\n"
    b"CREATE TABLE `wp_restored` (`id` int PRIMARY KEY);\n"
    b"INSERT INTO `wp_restored` VALUES (1),(2),(3);\n"
)
INDEX = b"<?php // restored index\n"


def host() -> str:
    return os.environ["DB_HOST"]


def port() -> int:
    return int(os.environ.get("DB_PORT", "3306"))


def root_connection(database: str | None = None) -> Connection:
    """CI's own connection: seeding, checksums and teardown only."""
    return pymysql.connect(
        host=host(),
        port=port(),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ["DB_PASSWORD"],
        database=database,
        autocommit=True,
    )


def config_text(*, name: str, user: str, password: str, db_host: str) -> str:
    return (
        "<?php\n"
        f"define('DB_NAME', {name!r});\n"
        f"define('DB_USER', {user!r});\n"
        f"define('DB_PASSWORD', {password!r});\n"
        f"define('DB_HOST', {db_host!r});\n"
        f"$table_prefix = {PREFIX!r};\n"
        "// distinct target content that must survive the restore\n"
    )


def sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


@dataclass
class LiveSite:
    """A Site Root, its ``wp-config.php`` and a throwaway live database."""

    site_root: Path
    config: Path
    database: str
    user: str
    password: str
    work: Path
    extra_users: list[str] = field(default_factory=list)

    def seed(self) -> None:
        with root_connection().cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{self.database}`")
            cursor.execute(f"USE `{self.database}`")
            for statement in WP_DUMP.decode().split(";\n"):
                if statement.strip():
                    cursor.execute(statement.replace("wp_restored", "wp_original"))
            cursor.execute("INSERT INTO wp_original VALUES (4),(5)")

    def write_config(self) -> None:
        self.config.write_text(
            config_text(
                name=self.database,
                user=self.user,
                password=self.password,
                db_host=host(),
            ),
            encoding="utf-8",
        )

    def databases(self) -> list[str]:
        with root_connection().cursor() as cursor:
            cursor.execute("SHOW DATABASES")
            return sorted(
                r[0] for r in cursor.fetchall() if r[0].startswith(self.database)
            )

    def checksums(self, database: str | None = None) -> dict[str, object]:
        with root_connection(database or self.database).cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]
            cursor.execute("CHECKSUM TABLE " + ", ".join(f"`{t}`" for t in tables))
            return {str(k).split(".")[-1]: v for k, v in cursor.fetchall()}

    def teardown(self) -> None:
        with root_connection().cursor() as cursor:
            for name in self.databases():
                cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            for user in self.extra_users:
                cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))


def make_site(base: Path, *, user: str, password: str) -> LiveSite:
    """A fresh Site Root under ``base`` (index, an old file, a config) and database."""
    root = base / "site"
    root.mkdir(parents=True)
    (root / "index.php").write_text("<?php\n// old index\n", encoding="utf-8")
    (root / "old.php").write_text("<?php\n// old\n", encoding="utf-8")
    site = LiveSite(
        site_root=root,
        config=root / "wp-config.php",
        database=f"wpk_{uuid.uuid4().hex[:10]}",
        user=user,
        password=password,
        work=base,
    )
    site.seed()
    site.write_config()
    return site


@pytest.fixture
def live_site(
    credential_sentinel: CredentialSentinel, live_account: str
) -> Iterator[LiveSite]:
    s = credential_sentinel
    site = LiveSite(
        site_root=s.site,
        config=s.config,
        database=f"wpk_{uuid.uuid4().hex[:10]}",
        user=live_account,
        password=s.password,
        work=s.workdir,
    )
    site.seed()
    site.write_config()
    try:
        yield site
    finally:
        site.teardown()


def build_archive(work: Path, *, config: str | None) -> Path:
    """A v1 Archive by the real writer: index, an in-tree config, the Dump."""
    source = work / f"source-{uuid.uuid4().hex[:6]}"
    source.mkdir()
    (source / "index.php").write_bytes(INDEX)
    if config is not None:
        (source / "wp-config.php").write_text(config, encoding="utf-8")
    members = list(collect_site_root(source, INCLUDE_EVERYTHING).members)
    dump = work / f"dump-{uuid.uuid4().hex[:6]}.sql"
    dump.write_bytes(WP_DUMP)
    members.insert(0, file_member(MemberRole.DUMP, DUMP_RELATIVE, dump))

    def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at="2026-11-03T00:00:00+00:00",
            backup_scope="all",
            source={"db_name": "source_db"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=True,
            config_path=None if config is None else "wp-config.php",
        )

    workspace = work / f"write-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(mode=0o700)
    path = work / f"archive-{uuid.uuid4().hex[:6]}.tar.gz"
    with path.open("wb") as out:
        write_archive(out, manifest, members, workspace=workspace, cipher=None)
    return path


@dataclass(frozen=True)
class RestoreSpec:
    """Everything one restore needs; picklable, for a spawned child."""

    site_root: str
    config_path: str
    archive: str
    mode: RestoreMode
    run_id: str
    user: str
    password: str
    relocation: RestoreRelocation
    relocated_user: str | None = None
    relocated_password: str | None = None
    state_home: str | None = None
    #: A throwaway administrator (E003-S02-004); CI's own when ``None``.
    admin_user: str | None = None
    admin_password: str | None = None
    #: The FR-7 waivers the plan is built under; every one by default.
    overrides: OverrideSet = field(
        default_factory=lambda: OverrideSet(waived=frozenset(Override))
    )
    #: Take a real Safety Snapshot of the live database (:func:`live_snapshot`).
    snapshot: bool = False
    #: Bind no administrator: the WordPress account alone (owner decision 55).
    no_admin: bool = False


Wrap = Callable[[str, object], object]


def _identity(_label: str, port_: object) -> object:
    return port_


def adapters_for(
    spec: RestoreSpec, secrets: SecretRegistry, wrap: Wrap = _identity
) -> tuple[AdapterSet, FileRunJournal]:
    """The real adapter set for ``spec``; ``wrap`` may wrap each command half."""
    from l3io.wp import database as wpdb

    # CI's administrator: ``ensure`` grants, which needs GRANT OPTION that
    # ``live_account`` lacks. The WordPress account is always the sentinel's.
    admin_spec = ExplicitCredentialSpec(
        os.environ.get("DB_ADMIN_USER", "root"), os.environ["DB_PASSWORD"]
    )
    if spec.admin_user is not None and spec.admin_password is not None:
        admin_spec = ExplicitCredentialSpec(spec.admin_user, spec.admin_password)
    admin = (
        None if spec.no_admin else admin_source(admin_spec, environ={}, secrets=secrets)
    )
    live = WpDatabase(
        wpdb.Explicit(spec.user, spec.password), secrets=secrets, admin=admin
    )
    restore = live
    if spec.relocated_user is not None and spec.relocated_password is not None:
        restore = WpDatabase(
            wpdb.Explicit(spec.relocated_user, spec.relocated_password),
            secrets=secrets,
            admin=admin,
        )
    config = WpConfigWriter(secrets)
    journal = FileRunJournal(journal_directory(Path(spec.site_root)))
    adapters = AdapterSet(
        archive_store_query=Unused("archive_store_query"),  # type: ignore[arg-type]
        archive_store_command=Unused("archive_store_command"),  # type: ignore[arg-type]
        archive_probe=TarArchiveProbe(),
        database_query=live,
        database_command=live,
        archive_writer=Unused("archive_writer"),  # type: ignore[arg-type]
        tracer=NoopTracer(),
        restore_database_query=restore,
        restore_database_command=wrap("database", restore),  # type: ignore[arg-type]
        site_files_query=LocalSiteFiles(),
        site_files_command=wrap("site_files", LocalSiteFiles()),  # type: ignore[arg-type]
        config_query=config,
        config_command=wrap("config", config),  # type: ignore[arg-type]
        search_replace=Unused("search_replace"),  # type: ignore[arg-type]
        journal_query=journal,
        journal_command=wrap("journal", journal),  # type: ignore[arg-type]
    )
    return adapters, journal


Preflight = Callable[[tuple[ToolRequirement, ...]], None]


def _no_preflight(_tools: tuple[ToolRequirement, ...]) -> None:
    return None


def ports_for(
    spec: RestoreSpec, adapters: AdapterSet, preflight: Preflight = _no_preflight
) -> RestorePorts:
    password = spec.relocated_password
    return restore_ports(
        adapters,
        preflight=preflight,
        free_bytes=lambda _path: 10**12,
        cutover_credentials=None if password is None else (lambda: password),
    )


def _located(spec: RestoreSpec) -> LocatedConfig | None:
    try:
        return locate_wp_config(Path(spec.site_root), stated=None)
    except Exception:
        return None


def _no_snapshot(_scope: object) -> SnapshotResult:
    msg = "the Safety Snapshot is skipped in these tests"
    raise AssertionError(msg)


def live_snapshot(
    adapters: AdapterSet, location: DatabaseLocation, path: Path
) -> Callable[[BackupScope], SnapshotResult]:
    """A real Safety Snapshot of the live database, as the WordPress account.

    The database is dumped through ``WpDatabase.dump_to`` and written by the
    real Archive writer with no Site files, owner-only, then verified by
    ``snapshot_result`` as the root's snapshot is.
    """

    def take(scope: BackupScope) -> SnapshotResult:
        work = path.parent / f"snapshot-build-{uuid.uuid4().hex[:6]}"
        work.mkdir(parents=True)
        dump = work / "dump.sql"
        adapters.database_command.dump_to(location, dump)
        members = [file_member(MemberRole.DUMP, DUMP_RELATIVE, dump)]

        def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
            return Manifest(
                format_version=FORMAT_VERSION,
                created_at="2026-11-03T00:00:00+00:00",
                backup_scope="database",
                source={"db_name": location.name},
                encrypted=False,
                payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
                members=index,
                dump_present=True,
                config_path=None,
            )

        workspace = work / "write"
        workspace.mkdir(mode=0o700)
        with path.open("wb") as out:
            write_archive(out, manifest, members, workspace=workspace, cipher=None)
        path.chmod(0o600)
        return snapshot_result(
            PurePosixPath(path),
            scope=scope,
            encrypted=False,
            size=path.stat().st_size,
            performed=True,
        )

    return take


def restore(
    spec: RestoreSpec,
    wrap: Wrap = _identity,
    *,
    extra_tools: tuple[ToolRequirement, ...] = (),
    preflight: Preflight = _no_preflight,
    select: Callable[[AdapterSet], AdapterSet] | None = None,
) -> ExecutionReport:
    """One restore of ``spec.archive`` onto ``spec.site_root``, as the root would.

    ``extra_tools`` reach the plan as the root's ``ext.required_tools()`` do;
    ``preflight`` replaces the default no-op; ``select`` (``dry_run_adapters``,
    say) is applied to the real set (E007-S01-004).
    """
    if spec.state_home is not None:
        os.environ["XDG_STATE_HOME"] = spec.state_home
    secrets = SecretRegistry()
    root = Path(spec.site_root)
    config_path = Path(spec.config_path)
    archive = StorageTarget(scheme="file", path=PurePosixPath(spec.archive))
    view, _ = verify_archive_view(Path(spec.archive))
    assert view is not None
    located = _located(spec)
    workspace = root.parent / f"workspace-{spec.run_id}"
    workspace.mkdir(mode=0o700)
    if located is not None:
        site, _ = site_descriptor_from_wp_config(
            root, config_file=located.path, environ={}, secrets=secrets
        )
    else:
        laid = workspace / "archive-wp-config.php"
        member = config_member(view)
        assert member is not None, "neither the target nor the Archive has a config"
        body = TarArchiveProbe().read_member(
            archive, view, member.path, max_bytes=1 << 20, payload=None
        )
        laid.write_bytes(body)
        site, _ = site_descriptor_from_archive_config(
            root.resolve(), config_file=laid, environ={}, secrets=secrets
        )
    adapters, _journal = adapters_for(spec, secrets, wrap)
    if select is not None:
        adapters = select(adapters)
    files = LocalSiteFiles()
    location = site.database_location()

    def survey_target() -> TargetState:
        survey = adapters.restore_database_query.survey_target(
            location, table_prefix=site.table_prefix, wordpress_user=None
        )
        return TargetState(
            site_root_empty=files.is_empty(root),
            database=survey,
            config_exists=config_path.exists(),
            site_root=site.site_root,
            site_root_entries=files.entry_count(root),
            database_location=location,
            load_target=None
            if spec.relocation.load_location is None
            else adapters.restore_database_query.survey_target(
                spec.relocation.load_location,
                table_prefix=site.table_prefix,
                wordpress_user=None,
            ),
            load_location=spec.relocation.load_location,
        )

    target = survey_target()
    snapshot_path = root.parent / "snapshots" / f"{spec.run_id}.tar.gz"
    plan = build_restore_plan(
        site,
        spec.mode,
        spec.overrides,
        view,
        target,
        archive=archive,
        relocation=spec.relocation,
        snapshot=SnapshotPolicy.TAKE if spec.snapshot else SnapshotPolicy.SKIP,
        snapshot_dir=root.parent / "snapshots",
        config_dir=config_path.parent,
        config_policy=ConfigPolicy.KEEP,
        extra_tools=extra_tools,
    )
    prior: dict[str, str | None] = dict.fromkeys(JOURNALED_CONSTANTS)
    if config_path.exists():
        read = adapters.config_query.read_constants(
            config_path, sorted(JOURNALED_CONSTANTS)
        )
        prior = {k: None if v is None else str(v) for k, v in read.items()}
    else:
        prior["DB_USER"] = spec.user
    load = load_database_name(site.db_name, spec.relocation)
    header = JournalHeader(
        run_id=spec.run_id,
        started_at="2026-11-03T00:00:00+00:00",
        site_root=site.site_root,
        restore_mode=spec.mode,
        archive=spec.archive,
        snapshot_path=str(snapshot_path) if spec.snapshot else None,
        live_database_before=site.db_name,
        staging_database=None,
        wp_config_path=str(config_path),
        load_database=load,
        cutover_keys=planned_cutover_keys(spec.relocation),
        cutover_targets=planned_cutover_targets(load, spec.relocation),
        prior_constants=prior,
        phase_undo={},
        # As the restore root: a URL relocation saves the config (decision 72).
        saved_config=str(root.parent / "snapshots" / f"{spec.run_id}-wp-config.php")
        if RestorePhase.SEARCH_REPLACE in plan.phases
        else None,
    )
    inputs = RestoreInputs(
        site=site,
        view=view,
        archive=archive,
        run_id=spec.run_id,
        relocation=spec.relocation,
        header=header,
        config_policy=ConfigPolicy.KEEP,
        config_path=config_path,
        payload=None,
        workspace=workspace,
        live_database_exists=target.database is not None
        and target.database.database_exists,
        rollback=database_rollback(site, spec.relocation, target, plan),
    )
    ports = ports_for(spec, adapters, preflight)
    if spec.snapshot:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    return execute_restore(
        plan,
        inputs,
        ports,
        live_snapshot(adapters, location, snapshot_path)
        if spec.snapshot
        else _no_snapshot,
        functools.partial(check_live, ports=ports),
        functools.partial(load_live, ports=ports, workspace=workspace),
        adapters.journal_command,
        None,
        survey_target=survey_target,
        opted_out_by="test: SnapshotPolicy.SKIP",
        undo_relocation=functools.partial(
            roll_back_relocation, ports=ports, workspace=workspace, cipher=None
        ),
    )


def read_keys(config: Path) -> dict[str, object]:
    """The four cutover keys through ``l3io.wp.config`` (the file must parse)."""
    values = WpConfigWriter(SecretRegistry()).read_constants(config, CUTOVER_KEYS)
    return dict(values)


def new_run_id() -> str:
    return str(uuid.uuid4())


def replace_spec(spec: RestoreSpec, **changes: object) -> RestoreSpec:
    return dataclasses.replace(spec, **changes)  # type: ignore[arg-type]


def spec_for(
    site: LiveSite,
    archive: Path,
    mode: RestoreMode,
    *,
    relocation: RestoreRelocation,
    relocated_user: str | None = None,
    relocated_password: str | None = None,
) -> RestoreSpec:
    return RestoreSpec(
        site_root=str(site.site_root),
        config_path=str(site.config),
        archive=str(archive),
        mode=mode,
        run_id=new_run_id(),
        user=site.user,
        password=site.password,
        relocation=relocation,
        relocated_user=relocated_user,
        relocated_password=relocated_password,
        state_home=os.environ.get("XDG_STATE_HOME"),
    )


def journal_for(site_root: Path) -> FileRunJournal:
    return FileRunJournal(journal_directory(site_root))


def survey_named(config: Path, *, user: str, password: str) -> object:
    """``survey_target`` of the database ``config`` names, as the administrator.

    ``user``/``password`` are ``live_account`` and the sentinel's password:
    the WordPress account ``WpDatabase`` is bound to, never a literal.
    """
    from l3io.wp import database as wpdb

    secrets = SecretRegistry()
    admin = admin_source(
        ExplicitCredentialSpec(
            os.environ.get("DB_ADMIN_USER", "root"), os.environ["DB_PASSWORD"]
        ),
        environ={},
        secrets=secrets,
    )
    keys = read_keys(config)
    database = WpDatabase(wpdb.Explicit(user, password), secrets=secrets, admin=admin)
    location = site_descriptor_from_wp_config(
        config.parent, config_file=config, environ={}, secrets=SecretRegistry()
    )[0].database_location()
    assert location.name == keys["DB_NAME"]
    return database.survey_target(location, table_prefix=PREFIX, wordpress_user=None)


# --------------------------------------------------------------------------
# The kill harness: a spawned child that stops after every durable effect
# --------------------------------------------------------------------------

#: The command Protocol each wrapped port implements (the source of truth).
WRAPPED: dict[str, type] = {
    "journal": RunJournalCommand,
    "site_files": SiteFilesCommand,
    "config": ConfigCommand,
    "database": DatabaseCommand,
}
#: The only members that are not kill points, each with its reason.
#: ``finish_run`` moves the record at the end of the Run; ``resolve`` is never
#: called by a restore; ``dump_to`` is the backup's, not the restore's.
NOT_SIGNALLED: dict[str, frozenset[str]] = {
    "journal": frozenset({"finish_run", "resolve"}),
    "site_files": frozenset(),
    "config": frozenset(),
    "database": frozenset({"dump_to"}),
}


def protocol_members(protocol: type) -> frozenset[str]:
    """The public methods ``protocol`` declares, its own or a base Protocol's."""
    return frozenset(
        name
        for klass in protocol.__mro__
        if klass.__module__ not in {"builtins", "typing", "typing_extensions"}
        for name, value in vars(klass).items()
        if not name.startswith("_") and callable(value)
    )


def signalled(
    wrapped: dict[str, type], excluded: dict[str, frozenset[str]]
) -> dict[str, frozenset[str]]:
    """Every member of each wrapped Protocol, minus its asserted exclusions."""
    assert set(excluded) == set(wrapped), (sorted(excluded), sorted(wrapped))
    result = {}
    for label, protocol in wrapped.items():
        members = protocol_members(protocol)
        stale = excluded[label] - members
        assert not stale, f"{label}: excluded but not a member: {sorted(stale)}"
        result[label] = members - excluded[label]
    return result


#: The methods after which the child signals an ordinal, per wrapped port:
#: every member of its command Protocol except :data:`NOT_SIGNALLED`'s. So a
#: member added to a Protocol later is a kill point without editing this file.
#: Journal: each durable record write. Commands: each effect, **after** it
#: lands and before it returns.
SIGNALLED: dict[str, frozenset[str]] = signalled(WRAPPED, NOT_SIGNALLED)


def _detail(label: str, name: str, args: tuple[object, ...]) -> str:
    """What an ordinal names: a journal event's phase or step, a write's keys."""
    if label == "journal" and name == "step_completed":
        return str(args[1])
    if label == "journal" and name in {"phase_started", "phase_completed"}:
        return str(getattr(args[0], "value", args[0]))
    if label == "config" and name == "set_constants":
        values = args[1]
        assert isinstance(values, dict)
        return ",".join(values)
    return ""


class _Signalling:
    """``inner`` with an ordinal sent to the parent after each signalled call."""

    def __init__(self, label: str, inner: object, conn: PipeEnd) -> None:
        self._label = label
        self._inner = inner
        self._conn = conn

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._inner, name)
        if name not in SIGNALLED[self._label] or not callable(attr):
            return attr

        def call(*args: object, **kwargs: object) -> object:
            result = attr(*args, **kwargs)
            detail = _detail(self._label, name, args)
            self._conn.send(("ordinal", f"{self._label}.{name}:{detail}"))
            if self._conn.recv() != "go":
                os._exit(3)
            return result

        return call


def child_main(conn: PipeEnd, spec: RestoreSpec) -> None:
    """The spawned child: one restore over signalling adapters."""
    try:
        restore(spec, wrap=lambda label, inner: _Signalling(label, inner, conn))
    except BaseException as exc:  # reported, never swallowed silently
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        return
    conn.send(("done", ""))


@dataclass
class Driven:
    """What one driven child did: its ordinal labels and how it ended."""

    labels: list[str]
    outcome: str


def drive(spec: RestoreSpec, *, kill_at: int | None, timeout: float = 120.0) -> Driven:
    """Run ``spec`` in a spawn child; SIGKILL it at ordinal ``kill_at``."""
    import multiprocessing
    import signal

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=child_main, args=(child, spec), daemon=True)
    process.start()
    child.close()
    labels: list[str] = []
    outcome = "killed"
    try:
        while True:
            if not parent.poll(timeout):
                outcome = "timeout"
                break
            kind, text = parent.recv()
            if kind != "ordinal":
                outcome = f"{kind}:{text}"
                break
            labels.append(text)
            if kill_at is not None and len(labels) == kill_at:
                os.kill(process.pid or 0, signal.SIGKILL)
                break
            parent.send("go")
    finally:
        if outcome == "timeout":
            os.kill(process.pid or 0, signal.SIGKILL)
        process.join(timeout)
        parent.close()
    if kill_at is not None:
        assert process.exitcode == -signal.SIGKILL, (outcome, labels)
    return Driven(labels=labels, outcome=outcome)
