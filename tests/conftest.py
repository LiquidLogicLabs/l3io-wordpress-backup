"""Fixtures shared by every test tier.

``credential_sentinel`` is AD-7 items 1-4 as a reusable fixture (ADR-0018).
It plants a unique sentinel password in a temporary ``wp-config.php`` and
captures every surface a credential could leak through; a test drives a Run
and calls :meth:`CredentialSentinel.assert_clean`. Its events are both the raw
ones (``capture_logs``, before any configuration) and those the configured
pipeline (``cli/logging.py``) handed to the stdlib root logger, already
redacted — so once ``main`` has configured logging, what is checked is what
the program actually emits.

``_reset_logging`` (autouse) undoes ``configure_logging`` after every test,
so one test's root handler never writes to another's closed stream.

Epic 3 story 3.1 applies the sentinel to a full restore; Epic 4
(E004-S01-003) applies its ``passphrase`` parameter to an encrypted Run.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import subprocess
import tarfile
import tempfile
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from l3io.wp.backup.ports.crypto import PayloadCipher

#: AD-26's environment step; spelled out so the fixture does not import the CLI.
PASSPHRASE_ENV = "L3IO_WP_BACKUP_PASSPHRASE"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(repr=False)
class CredentialSentinel:
    """A planted secret, and everything captured while a Run used it."""

    password: str
    passphrase: str | None
    site: Path
    config: Path
    workdir: Path
    environ_before: dict[str, str]
    caplog: pytest.LogCaptureFixture
    raw: list[dict[str, Any]]
    monkeypatch: pytest.MonkeyPatch = field(repr=False)
    popen: list[tuple[object, object]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)
    #: A second planted password, for a relocated account (E003-S02-005): the
    #: cutover's ``DB_PASSWORD``. Unique per test and in :attr:`secrets`.
    relocated_password: str = field(
        default_factory=lambda: f"sentinel-pw2-{uuid.uuid4().hex}"
    )
    #: A third, for a throwaway administrator account (E003-S02-004): the
    #: database restore's ``--admin-password``. Unique per test, in :attr:`secrets`.
    admin_password: str = field(
        default_factory=lambda: f"sentinel-pw3-{uuid.uuid4().hex}"
    )

    def __repr__(self) -> str:
        # pytest prints a failing frame's arguments: never the sentinels.
        return f"CredentialSentinel(<{len(self.texts)} captured texts>)"

    @property
    def logs(self) -> list[dict[str, Any]]:
        """Raw captured events, then every event the configured pipeline emitted."""
        return [*self.raw, *pipeline_events(self.caplog)]

    @property
    def secrets(self) -> tuple[str, ...]:
        extra = () if self.passphrase is None else (self.passphrase,)
        return (self.password, self.relocated_password, self.admin_password, *extra)

    def add(self, surface: str, text: str | bytes) -> None:
        """Capture ``text`` (CLI output, Manifest bytes, …) under ``surface``."""
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        self.texts.append((surface, text))

    def add_exception(self, surface: str, exc: BaseException) -> None:
        """``str``, ``repr`` and the formatted traceback, with no redaction."""
        self.add(f"{surface}: str", str(exc))
        self.add(f"{surface}: repr", repr(exc))
        self.add(f"{surface}: traceback", "".join(traceback.format_exception(exc)))

    def add_repr(self, surface: str, value: object) -> None:
        self.add(f"{surface}: repr", repr(value))

    def watch(self, owner: object, name: str) -> None:
        """Wrap ``owner.name`` so each call's arguments and result are captured."""
        real: Callable[..., Any] = getattr(owner, name)

        def spy(*args: object, **kwargs: object) -> object:
            for i, arg in enumerate(args):
                self.add_repr(f"{name} arg {i}", arg)
            for key, arg in kwargs.items():
                self.add_repr(f"{name} {key}=", arg)
            result = real(*args, **kwargs)
            self.add_repr(f"{name} result", result)
            return result

        self.monkeypatch.setattr(owner, name, spy)

    def surfaces(self) -> list[tuple[str, str]]:
        found = list(self.texts)
        renderer = structlog.processors.KeyValueRenderer()
        for event in self.logs:
            found.append(("structlog event", repr(event)))
            found.append(("structlog rendered", str(renderer(None, "", dict(event)))))
        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
        for record in self.caplog.records:
            found.append(("stdlib record", formatter.format(record)))
            found.append(("stdlib record fields", repr(vars(record))))
        found.append(("caplog text", self.caplog.text))
        for argv, env in self.popen:
            found.append(("Popen argv", repr(argv)))
            found.append(("Popen env", repr(env)))
        # The variable the fixture itself planted is the one expected holder.
        planted = PASSPHRASE_ENV if self.passphrase is not None else None
        environ = {k: v for k, v in os.environ.items() if k != planted}
        found.append(("os.environ after", repr(environ)))
        return found

    def leaks(self) -> list[str]:
        """Each surface holding a sentinel, named with its fingerprint only."""
        return [
            f"{surface} holds sha256:{_fingerprint(secret)}"
            for surface, text in self.surfaces()
            for secret in self.secrets
            if secret in text
        ]

    def assert_clean(self) -> None:
        leaks = self.leaks()
        assert not leaks, f"credential reached {len(leaks)} surface(s): {leaks}"
        after, before = _environ(), self.environ_before
        changed = sorted(
            k for k in after.keys() | before.keys() if after.get(k) != before.get(k)
        )
        # Names only: the environment can hold real secrets (CI's DB_PASSWORD).
        assert not changed, f"the Run changed os.environ: {changed}"
        assert not any(
            isinstance(env, Mapping) and "MYSQL_PWD" in env for _, env in self.popen
        ), "MYSQL_PWD was put in a child's environment"


def pipeline_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The event dicts ``cli/logging.py``'s pipeline handed to stdlib logging."""
    return [dict(r.msg) for r in caplog.records if isinstance(r.msg, dict)]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    root = logging.getLogger()
    level = root.level
    yield
    from l3io.wp.backup.cli.logging import reset_logging

    reset_logging()
    root.setLevel(level)


@pytest.fixture(autouse=True)
def _isolated_state_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every Run's Site Lock (AD-19) lands in a per-test state directory.

    ``platformdirs`` reads ``XDG_STATE_HOME``; a subprocess inherits it. The
    directory is a sibling of ``tmp_path``, never inside a test's Site Root.
    """
    state = tmp_path_factory.mktemp("xdg-state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def _environ() -> dict[str, str]:
    """``os.environ`` without the variable pytest itself rewrites per phase."""
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


def write_wp_config(path: Path, *, password: str, user: str | None = None) -> None:
    """A ``wp-config.php`` for the CI database (or ``db.invalid``)."""
    host = os.environ.get("DB_HOST", "db.invalid")
    user = user or os.environ.get("DB_USER", "wp")
    path.write_text(
        "<?php\n"
        f"define('DB_NAME', {os.environ.get('DB_NAME', 'wptest')!r});\n"
        f"define('DB_USER', {user!r});\n"
        f"define('DB_PASSWORD', {password!r});\n"
        f"define('DB_HOST', {host!r});\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )


@pytest.fixture
def credential_sentinel(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[CredentialSentinel]:
    """A planted sentinel password, with every leak surface under capture.

    Parameter (``indirect=True``, optional): ``{"passphrase": True}`` plants a
    sentinel in ``L3IO_WP_BACKUP_PASSPHRASE``. The password is always
    generated, never taken from the environment, so it is unique. It is
    plain ASCII: the wp-config reader does not unescape a quoted PHP string,
    so escaped spellings are covered by ``tests/unit/adapters/test_redaction.py``.
    """
    params: dict[str, Any] = getattr(request, "param", None) or {}
    password = f"sentinel-pw-{uuid.uuid4().hex}"
    passphrase = f"sentinel-pp-{uuid.uuid4().hex}" if params.get("passphrase") else None
    if passphrase is not None:
        monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    config = site / "wp-config.php"
    write_wp_config(config, password=password)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(workdir))
    monkeypatch.setattr(tempfile, "tempdir", None)
    caplog.set_level(1)
    with capture_logs() as logs:
        sentinel = CredentialSentinel(
            password=password,
            passphrase=passphrase,
            site=site,
            config=config,
            workdir=workdir,
            environ_before=_environ(),
            caplog=caplog,
            raw=logs,
            monkeypatch=monkeypatch,
        )
        real_popen = subprocess.Popen
        signature = inspect.signature(real_popen)

        class SpyPopen(real_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args: object, **kwargs: object) -> None:
                bound = signature.bind(*args, **kwargs).arguments
                sentinel.popen.append((bound.get("args"), bound.get("env")))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", SpyPopen)
        planted = {
            k: v for k, v in sentinel.environ_before.items() if k != PASSPHRASE_ENV
        }
        inputs = (repr(planted), str(tmp_path))
        assert not any(
            secret in text for secret in sentinel.secrets for text in inputs
        ), "a planted sentinel already occurs in the fixture's own inputs"
        yield sentinel


# --------------------------------------------------------------------------
# A passphrase-encrypted Archive, and a forged one (BL-E002-060)
# --------------------------------------------------------------------------


def seal_archive(workdir: Path, passphrase: str | None) -> Path:
    """A small v1 Archive by the real writer; encrypted when ``passphrase`` is set."""
    from l3io.wp.backup.adapters.crypto import PassphraseCipher
    from l3io.wp.backup.adapters.tar import (
        collect_site_root,
        file_member,
        write_archive,
    )
    from l3io.wp.backup.domain.archive_format import (
        DUMP_RELATIVE,
        FORMAT_VERSION,
        PAYLOAD_MEMBER,
        Manifest,
        ManifestMember,
        ManifestPayload,
        MemberRole,
    )
    from l3io.wp.backup.domain.exclusion import INCLUDE_EVERYTHING

    def manifest(index: tuple[ManifestMember, ...]) -> Manifest:
        return Manifest(
            format_version=FORMAT_VERSION,
            created_at="2026-11-03T00:00:00+00:00",
            backup_scope="all",
            source={"db_name": "wptest"},
            encrypted=False,
            payload=ManifestPayload(name=PAYLOAD_MEMBER, size=0, sha256="0" * 64),
            members=index,
            dump_present=True,
        )

    stem = "plain" if passphrase is None else "sealed"
    site = workdir / f"{stem}-site"
    site.mkdir()
    (site / "index.php").write_bytes(b"<?php // the real index\n")
    dump = workdir / f"{stem}-dump.sql"
    dump.write_bytes(b"CREATE TABLE wp_users (id INT);\n")
    workspace = workdir / f"{stem}-workspace"
    workspace.mkdir(mode=0o700)
    members = [
        file_member(MemberRole.DUMP, DUMP_RELATIVE, dump),
        *collect_site_root(site, INCLUDE_EVERYTHING).members,
    ]
    cipher = None if passphrase is None else PassphraseCipher(passphrase)
    path = workdir / f"{stem}.tar.gz"
    with path.open("wb") as out:
        written = write_archive(
            out, manifest, members, workspace=workspace, cipher=cipher
        )
    assert written.encrypted == (passphrase is not None)
    return path


def forge_archive(
    archive: Path,
    out: Path,
    edit: Callable[[bytes], bytes],
    *,
    manifest_edit: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """The red-team forgery (RT-H2): edit the payload, recompute the Manifest's
    ``size`` and ``sha256``, rewrite the Archive. Needs no key.

    ``manifest_edit`` then edits the outer Manifest's parsed JSON in place
    (BL-E002-115: a Manifest changed outside the payload)."""
    import io
    import json
    import tarfile

    with tarfile.open(archive) as outer:
        names = outer.getnames()
        manifest_name, payload_name = names[0], names[1]
        manifest_file = outer.extractfile(manifest_name)
        payload_file = outer.extractfile(payload_name)
        assert manifest_file is not None
        assert payload_file is not None
        manifest = json.loads(manifest_file.read())
        payload = edit(payload_file.read())
    manifest["payload"]["size"] = len(payload)
    manifest["payload"]["sha256"] = hashlib.sha256(payload).hexdigest()
    if manifest_edit is not None:
        manifest_edit(manifest)
    text = json.dumps(manifest).encode()
    with tarfile.open(out, "w") as forged:
        for name, data in ((manifest_name, text), (payload_name, payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            forged.addfile(info, io.BytesIO(data))
    return out


InnerMembers = list[tuple[tarfile.TarInfo, bytes | None]]


def reseal(
    cipher: PayloadCipher, edit: Callable[[InnerMembers], InnerMembers]
) -> Callable[[bytes], bytes]:
    """A ``forge_archive`` edit that rewrites an encrypted payload's inner tar.

    Decrypts with ``cipher``, hands ``edit`` the inner ``(TarInfo, bytes)``
    members in order (``None`` for a non-file) and re-encrypts what it
    returns: a forger who holds the passphrase (BL-E002-115's layout cases).
    """
    import io
    import tarfile

    def forge(payload: bytes) -> bytes:
        plain = io.BytesIO()
        cipher.decrypt(io.BytesIO(payload), plain)
        members: InnerMembers = []
        with tarfile.open(fileobj=io.BytesIO(plain.getvalue()), mode="r:gz") as inner:
            for info in inner.getmembers():
                handle = inner.extractfile(info) if info.isfile() else None
                members.append((info, None if handle is None else handle.read()))
        rebuilt = io.BytesIO()
        with tarfile.open(fileobj=rebuilt, mode="w:gz") as out:
            for info, body in edit(members):
                out.addfile(info, None if body is None else io.BytesIO(body))
        sealed = io.BytesIO()
        cipher.encrypt(io.BytesIO(rebuilt.getvalue()), sealed)
        return sealed.getvalue()

    return forge


def backdoor_plain_payload(payload: bytes) -> bytes:
    """A plain payload with ``site-root/index.php`` replaced by a backdoor."""
    import io
    import tarfile

    rebuilt = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as inner,
        tarfile.open(fileobj=rebuilt, mode="w:gz") as out,
    ):
        for info in inner.getmembers():
            data = inner.extractfile(info) if info.isfile() else None
            body = None if data is None else data.read()
            if info.name == "site-root/index.php":
                body = b"<?php system($_GET['c']);\n"
                info.size = len(body)
            out.addfile(info, None if body is None else io.BytesIO(body))
    return rebuilt.getvalue()


@pytest.fixture
def live_account(credential_sentinel: CredentialSentinel) -> Iterator[str]:
    """A throwaway CI-database account whose password is the sentinel.

    Shared by the live credential-hygiene tier and the live FR-7 refusal test;
    needs ``DB_HOST`` (the caller skips without it). The sentinel's
    ``wp-config.php`` is rewritten to log in as this account.

    CI's own account is not used: its password is in the job environment and
    is not unique, so it could not be told apart from a leak. The CI root
    connection only creates and drops the account.
    """
    pymysql = pytest.importorskip("pymysql")
    user = f"wpb_{uuid.uuid4().hex[:12]}"
    root = pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ.get("DB_USER", "root"),
        password=os.environ["DB_PASSWORD"],
        autocommit=True,
    )
    with root.cursor() as cursor:
        cursor.execute(
            "CREATE USER %s@'%%' IDENTIFIED BY %s", (user, credential_sentinel.password)
        )
        cursor.execute("GRANT ALL PRIVILEGES ON *.* TO %s@'%%'", (user,))
    write_wp_config(
        credential_sentinel.config, password=credential_sentinel.password, user=user
    )
    try:
        yield user
    finally:
        with root.cursor() as cursor:
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (user,))
        root.close()
