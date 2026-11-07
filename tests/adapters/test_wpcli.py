"""E007-S01-003 (FR-12, ADR-0005, AD-7, AD-11) — ``WpCliSearchReplace``.

A recording runner returns canned WP-CLI 2.12.0 output, so every argument
vector, working directory and environment the adapter builds is asserted
without ``wp``. The real WP-CLI is ``tests/integration/test_search_replace.py``.

Covers BL-E002-117's leak test: sentinels planted in the process environment
(the credential variables ``cli/passphrase.py`` and ``cli/credentials.py``
read, found by scanning their source, plus arbitrary names) reach no child's
environment or argv, and every child's environment keys are exactly
:data:`WP_CLI_ENV_KEYS` plus ``WP_CLI_CONFIG_PATH``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import l3io.wp.backup.adapters.wpcli as wpcli
from l3io.wp.backup.adapters.journal import journal_directory
from l3io.wp.backup.adapters.preflight import _probe_environment
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import (
    WP_CLI_CONFIG_FILES,
    WP_CLI_ENV_KEYS,
    WpCliSearchReplace,
)
from l3io.wp.backup.domain.errors import (
    InterferingReplacementsError,
    RefusedError,
    SearchReplaceIncompleteError,
    StoredUrlInvalidError,
    StoredUrlMissingError,
    WpCliConfigFoundError,
    WpCliFailedError,
    WpCliOutputUnrecognisedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.search_replace import (
    Replacement,
    SearchReplaceReport,
    UrlRelocation,
)

WP = "/usr/local/bin/wp"
OLD = "http://old.example.com"
NEW = "https://new.example.org/site"
SRC = Path(wpcli.__file__).resolve().parents[1]
HEADER = "Table\tColumn\tReplacements\tType"


def _table(rows: list[tuple[str, str, int]]) -> str:
    total = sum(n for _, _, n in rows)
    body = [HEADER, *(f"{t}\t{c}\t{n}\tPHP" for t, c, n in rows)] if rows else []
    noun = "replacement" if total == 1 else "replacements"
    return "\n".join([*body, f"Success: {total} {noun} to be made."]) + "\n"


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    config_seen: tuple[bool, int, int] | None = None


@dataclass
class FakeWp:
    """Canned WP-CLI output by step; records every call."""

    siteurl: str | None = OLD
    home: str | None = OLD
    previews: dict[str, str] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)
    recounts: dict[str, int] = field(default_factory=dict)
    fail: Callable[[list[str]], tuple[int, str] | None] = lambda _argv: None
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        config = Path(env.get("WP_CLI_CONFIG_PATH", "/nonexistent"))
        seen = None
        if config.is_file():
            st = config.stat()
            seen = (True, st.st_size, stat.S_IMODE(st.st_mode))
        self.calls.append(Call(list(argv), cwd, dict(env), seen))
        failed = self.fail(argv)
        if failed is not None:
            return subprocess.CompletedProcess(argv, failed[0], "", failed[1])
        return subprocess.CompletedProcess(argv, 0, self._stdout(argv), "")

    def _stdout(self, argv: list[str]) -> str:
        if argv[-2:] == ["cli", "version"]:
            return "WP-CLI 2.12.0\n"
        if "option" in argv:
            name = next(a for a in argv if a.startswith("--search=")).split("=", 1)[1]
            value = {"siteurl": self.siteurl, "home": self.home}[name]
            rows = (
                [] if value is None else [{"option_name": name, "option_value": value}]
            )
            return json.dumps(rows)
        old = argv[argv.index("search-replace") + 1]
        if "--format=count" in argv:
            table = self.recounts if "--dry-run" in argv else self.replaced
            return f"{table.get(old, 0)}\n"
        return self.previews.get(old, _table([]))

    def steps(self) -> list[str]:
        out = []
        for call in self.calls:
            argv = call.argv
            if argv[-2:] == ["cli", "version"]:
                out.append("version")
            elif "option" in argv:
                out.append("read_urls")
            elif "--format=count" in argv:
                out.append("recount" if "--dry-run" in argv else "replace")
            else:
                out.append("preview")
        return out


@pytest.fixture
def site_root(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    return root


@pytest.fixture
def not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


def _one_pair_wp(
    *, count: int = 3, tables: tuple[str, ...] = ("wp_options",)
) -> FakeWp:
    rows = [(t, "option_value", 1) for t in tables]
    rows[0] = (rows[0][0], rows[0][1], count - (len(tables) - 1))
    return FakeWp(previews={OLD: _table(rows)}, replaced={OLD: count})


def _relocate(
    fake: FakeWp, site_root: Path, urls: UrlRelocation | None = None
) -> SearchReplaceReport:
    replacer = WpCliSearchReplace(WP, secrets=SecretRegistry(), run=fake)
    return replacer.relocate_content(site_root, urls or UrlRelocation(NEW, NEW))


# --------------------------------------------------------------------------
# Argument vectors, steps, report
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("not_root")
def test_the_exact_argv_of_every_step(site_root: Path) -> None:
    fake = _one_pair_wp(count=3, tables=("wp_options", "wp_postmeta"))
    report = _relocate(fake, site_root)
    base = [
        WP,
        f"--path={site_root}",
        "--skip-plugins",
        "--skip-themes",
        "--skip-packages",
        "--no-color",
    ]
    pair = [
        "search-replace",
        OLD,
        NEW,
        "--precise",
        "--all-tables-with-prefix",
        "--skip-columns=guid",
        "--report-changed-only",
    ]
    assert [c.argv for c in fake.calls] == [
        [*base, "cli", "version"],
        [*base, "option", "list", "--search=siteurl", "--format=json"],
        [*base, "option", "list", "--search=home", "--format=json"],
        [*base, *pair, "--dry-run"],
        [*base, *pair, "--format=count"],
        [*base, *pair, "--dry-run", "--format=count"],
    ]
    assert report == SearchReplaceReport(
        pairs=(Replacement(OLD, NEW),),
        replacements=3,
        tables=("wp_options", "wp_postmeta"),
        remaining=0,
        merged=True,
    )


@pytest.mark.parametrize("encrypted", [True, False])
def test_allow_root_and_a_warning_only_as_root(
    site_root: Path, monkeypatch: pytest.MonkeyPatch, encrypted: bool
) -> None:
    facts = wpcli.RunFacts()
    facts.archive_encrypted = encrypted
    for euid in (0, 1000):
        monkeypatch.setattr(os, "geteuid", lambda euid=euid: euid)
        fake = _one_pair_wp()
        replacer = WpCliSearchReplace(
            WP, secrets=SecretRegistry(), facts=facts, run=fake
        )
        with capture_logs() as logs:
            report = replacer.relocate_content(site_root, UrlRelocation(NEW, None))
        assert report.replacements == 3, "the Run proceeds as root"
        flagged = ["--allow-root" in c.argv for c in fake.calls]
        assert all(flagged) if euid == 0 else not any(flagged)
        warnings = [e for e in logs if e["event"] == "wp_cli_running_as_root"]
        if euid == 0:
            assert len(warnings) == 1
            assert warnings[0]["log_level"] == "warning"
            assert warnings[0]["site_root"] == str(site_root)
            assert warnings[0]["archive_encrypted"] is encrypted
        else:
            assert warnings == []


@pytest.mark.usefixtures("not_root")
def test_the_preview_table_gives_counts_and_tables(site_root: Path) -> None:
    second = "https://old.example.com/wp"
    fake = FakeWp(
        siteurl=second,
        home=OLD,
        previews={
            second: _table([("wp_options", "option_value", 2)]),
            OLD: _table(
                [
                    ("wp_options", "option_value", 4),
                    ("wp_posts", "post_content", 5),
                    ("wp_plugin_x", "data", 1),
                ]
            ),
        },
        replaced={second: 2, OLD: 10},
    )
    report = _relocate(fake, site_root, UrlRelocation(NEW + "/wp", NEW))
    assert report.pairs == (Replacement(second, NEW + "/wp"), Replacement(OLD, NEW))
    assert report.replacements == 12
    assert report.tables == ("wp_options", "wp_plugin_x", "wp_posts")
    assert report.merged is False
    # Every read and preview precedes the first replacement.
    steps = fake.steps()
    assert steps.index("replace") > max(
        i for i, s in enumerate(steps) if s in {"read_urls", "preview"}
    )


@pytest.mark.usefixtures("not_root")
def test_an_empty_preview_is_zero_and_no_table(site_root: Path) -> None:
    fake = FakeWp(previews={OLD: _table([])}, replaced={OLD: 0})
    report = _relocate(fake, site_root)
    assert (report.replacements, report.tables) == (0, ())


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize(
    "output",
    [
        "Tbl\tColumn\tReplacements\tType\nwp_options\tv\t1\tPHP\n"
        "Success: 1 replacement to be made.\n",
        f"{HEADER}\nwp_options\tv\tmany\tPHP\nSuccess: 1 replacement to be made.\n",
        f"{HEADER}\nwp_options\tv\t1\n Success: 1 replacement to be made.\n",
        f"{HEADER}\nwp_options\tv\t1\tPHP\n",
        f"{HEADER}\nwp_options\tv\t1\tPHP\nSuccess: 2 replacements to be made.\n",
        "Success: 3 replacements to be made.\n",
        "Success: lots\n",
        "",
    ],
    ids=[
        "header",
        "count",
        "columns",
        "no-success",
        "sum",
        "total-no-table",
        "success-no-count",
        "empty",
    ],
)
def test_unrecognised_preview_raises_before_any_replace(
    site_root: Path, output: str
) -> None:
    fake = FakeWp(previews={OLD: output})
    with pytest.raises(WpCliOutputUnrecognisedError) as caught:
        _relocate(fake, site_root)
    assert caught.value.step == "preview"
    assert caught.value.wp_cli_version == "2.12.0"
    assert caught.value.replaced_maybe is False
    assert "nothing was replaced" in str(caught.value)
    assert "WP-CLI WP-CLI" not in str(caught.value)
    assert "replace" not in fake.steps()
    assert caught.value.exit_code is ExitCode.FAILED


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize("missing", ["siteurl", "home"])
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_stored_url_fails_before_any_write(
    site_root: Path, missing: str, value: str | None
) -> None:
    fake = FakeWp(**{missing: value})  # type: ignore[arg-type]
    with pytest.raises(StoredUrlMissingError) as caught:
        _relocate(fake, site_root)
    assert caught.value.option == missing
    assert "preview" not in fake.steps()
    assert "replace" not in fake.steps()


@pytest.mark.usefixtures("not_root")
def test_option_list_that_is_not_json_is_unrecognised(site_root: Path) -> None:
    class NotJson(FakeWp):
        def _stdout(self, argv: list[str]) -> str:
            return "Warning: x" if "option" in argv else super()._stdout(argv)

    with pytest.raises(WpCliOutputUnrecognisedError) as caught:
        _relocate(NotJson(), site_root)
    assert caught.value.step == "read_urls"


@pytest.mark.usefixtures("not_root")
def test_a_non_zero_recount_is_incomplete(site_root: Path) -> None:
    fake = _one_pair_wp()
    fake.recounts = {OLD: 2}
    with pytest.raises(SearchReplaceIncompleteError) as caught:
        _relocate(fake, site_root)
    assert (caught.value.old, caught.value.new, caught.value.remaining) == (OLD, NEW, 2)


@pytest.mark.usefixtures("not_root")
def test_a_nested_pair_skips_the_recount_with_a_warning(site_root: Path) -> None:
    new = OLD + "/blog"
    fake = _one_pair_wp()
    fake.recounts = {OLD: 9}  # would fail the check: the new URL holds the old
    with capture_logs() as logs:
        report = _relocate(fake, site_root, UrlRelocation(new, new))
    assert "recount" not in fake.steps()
    assert report.remaining is None
    skipped = [e for e in logs if e["event"] == "post_check_skipped"]
    assert len(skipped) == 1
    assert (skipped[0]["old"], skipped[0]["new"]) == (OLD, new)


@pytest.mark.usefixtures("not_root")
def test_nothing_to_do_replaces_nothing(site_root: Path) -> None:
    fake = FakeWp()
    report = _relocate(fake, site_root, UrlRelocation(OLD, OLD))
    assert report == SearchReplaceReport(
        pairs=(), replacements=0, tables=(), remaining=0, merged=True
    )
    assert not any("search-replace" in c.argv for c in fake.calls)


@pytest.mark.usefixtures("not_root")
def test_count_drift_is_warned_and_the_actual_count_reported(site_root: Path) -> None:
    fake = _one_pair_wp(count=3)
    fake.replaced = {OLD: 5}
    with capture_logs() as logs:
        report = _relocate(fake, site_root)
    assert report.replacements == 5
    drift = [e for e in logs if e["event"] == "search_replace_count_drift"]
    assert [(e["previewed"], e["replaced"]) for e in drift] == [(3, 5)]


@pytest.mark.usefixtures("not_root")
def test_completion_and_each_call_are_logged(site_root: Path) -> None:
    fake = _one_pair_wp()
    with capture_logs() as logs:
        _relocate(fake, site_root)
    invoked = [e for e in logs if e["event"] == "wp_cli_invoked"]
    assert [e["step"] for e in invoked] == fake.steps()
    for event in invoked:
        assert event["log_level"] == "debug"
        assert event["exit_status"] == 0
        assert isinstance(event["duration_ms"], int)
        assert event["argv"][0] == WP
    (done,) = [e for e in logs if e["event"] == "search_replace_completed"]
    assert done["log_level"] == "info"
    assert done["pairs"] == [{"old": OLD, "new": NEW}]
    assert done["replacements"] == 3
    assert done["tables"] == ["wp_options"]
    assert done["remaining"] == 0
    assert done["merged"] is True
    assert done["wp_cli_version"] == "2.12.0"


# --------------------------------------------------------------------------
# Failure: exit status, OSError, redaction
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("not_root")
def test_a_failing_wp_raises_with_a_redacted_tail(site_root: Path) -> None:
    secret = f"sentinel-{uuid.uuid4().hex}"
    secrets = SecretRegistry()
    secrets.register(secret, source="test")
    lines = [f"line {i}" for i in range(30)] + [f"Error: access denied for {secret}"]

    def fail(argv: list[str]) -> tuple[int, str] | None:
        return (1, "\n".join(lines)) if "--format=count" in argv else None

    fake = _one_pair_wp()
    fake.fail = fail
    replacer = WpCliSearchReplace(WP, secrets=secrets, run=fake)
    with capture_logs() as logs, pytest.raises(WpCliFailedError) as caught:
        replacer.relocate_content(site_root, UrlRelocation(NEW, None))
    error = caught.value
    assert (error.step, error.exit_status) == ("replace", 1)
    assert secret not in str(error)
    assert error.stderr_tail.splitlines()[0] == "line 11"
    assert len(error.stderr_tail.splitlines()) == 20
    assert secret not in repr(logs)


@pytest.mark.usefixtures("not_root")
def test_a_wp_that_cannot_start_is_a_failure(site_root: Path) -> None:
    def run(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    replacer = WpCliSearchReplace(WP, secrets=SecretRegistry(), run=run)
    with pytest.raises(WpCliFailedError) as caught:
        replacer.relocate_content(site_root, UrlRelocation(NEW, None))
    assert (caught.value.step, caught.value.exit_status) == ("version", None)


@pytest.mark.usefixtures("not_root")
def test_no_registered_secret_in_any_argv_or_record(site_root: Path) -> None:
    secret = f"sentinel-{uuid.uuid4().hex}"
    secrets = SecretRegistry()
    secrets.register(secret, source="test")
    fake = _one_pair_wp()
    with capture_logs() as logs:
        WpCliSearchReplace(WP, secrets=secrets, run=fake).relocate_content(
            site_root, UrlRelocation(NEW, NEW)
        )
    for call in fake.calls:
        assert secret not in " ".join(call.argv)
        assert secret not in repr(call.env)
    assert secret not in repr(logs)


# --------------------------------------------------------------------------
# Working directory and WP-CLI config (E007 gate C1)
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("not_root")
def test_every_call_runs_in_a_private_directory_under_the_state_dir(
    site_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The test's state directory is itself under /tmp, so $TMPDIR is moved
    # beside it: a cwd from mkdtemp() without dir= would land there.
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr(tempfile, "tempdir", None)
    fake = _one_pair_wp()
    _relocate(fake, site_root)
    state = journal_directory(site_root)
    tmp = Path(tempfile.gettempdir()).resolve()
    cwds = {c.cwd for c in fake.calls}
    assert len(cwds) == 1
    (cwd,) = cwds
    assert cwd.parent == state
    assert not cwd.resolve().is_relative_to(tmp)
    assert not cwd.resolve().is_relative_to(site_root.resolve())
    assert not cwd.exists(), "the directory is removed afterwards"


@pytest.mark.usefixtures("not_root")
def test_the_directory_is_0700_and_removed_on_failure(site_root: Path) -> None:
    modes: list[int] = []

    def run(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        modes.append(stat.S_IMODE(cwd.stat().st_mode))
        return subprocess.CompletedProcess(argv, 3, "", "boom")

    replacer = WpCliSearchReplace(WP, secrets=SecretRegistry(), run=run)
    with pytest.raises(WpCliFailedError):
        replacer.relocate_content(site_root, UrlRelocation(NEW, None))
    assert modes == [0o700]
    assert list(journal_directory(site_root).glob("wp-cli-*")) == []


@pytest.mark.usefixtures("not_root")
def test_wp_cli_config_path_is_an_empty_file_in_cwd(site_root: Path) -> None:
    fake = _one_pair_wp()
    _relocate(fake, site_root)
    for call in fake.calls:
        config = Path(call.env["WP_CLI_CONFIG_PATH"])
        assert config.parent == call.cwd
        assert call.config_seen == (True, 0, 0o600)


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize("name", WP_CLI_CONFIG_FILES)
@pytest.mark.parametrize("depth", [0, 1, 3])
def test_a_wp_cli_config_in_an_ancestor_is_refused_before_any_call(
    site_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    depth: int,
) -> None:
    state = tmp_path / "home" / "state" / "snapshots" / "digest"
    monkeypatch.setattr(wpcli, "journal_directory", lambda _root: state)
    # depth 0: the state directory itself; the private directory is below it.
    planted = state.parents[depth - 1] if depth else state
    planted.mkdir(parents=True, exist_ok=True)
    (planted / name).write_text("require:\n  - marker.php\n", encoding="utf-8")
    fake = _one_pair_wp()
    with pytest.raises(WpCliConfigFoundError) as caught:
        _relocate(fake, site_root)
    assert caught.value.path == planted / name
    assert isinstance(caught.value, RefusedError)
    assert fake.calls == []


# --------------------------------------------------------------------------
# BL-E002-117: the explicit minimal environment, and the leak test
# --------------------------------------------------------------------------


def _credential_variables() -> set[str]:
    """Every ``L3IO_WP_BACKUP_*`` name the credential modules read (their source)."""
    names: set[str] = set()
    for module in ("cli/passphrase.py", "cli/credentials.py"):
        text = (SRC / module).read_text(encoding="utf-8")
        names |= set(re.findall(r"\bL3IO_WP_BACKUP_[A-Z0-9_]+\b", text))
    return names


def test_the_credential_scan_finds_the_passphrase_and_admin_password() -> None:
    names = _credential_variables()
    assert "L3IO_WP_BACKUP_PASSPHRASE" in names
    assert "L3IO_WP_BACKUP_ADMIN_PASSWORD" in names


def test_the_env_keys_are_preflights_probe_keys() -> None:
    assert set(_probe_environment({})) == set(WP_CLI_ENV_KEYS)


@pytest.mark.usefixtures("not_root")
def test_no_sentinel_reaches_a_wp_child(
    site_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = {
        *_credential_variables(),
        "MYSQL_PWD",
        "MYSQL_HOST",
        "AGE_PASSPHRASE",
        "WP_CLI_PACKAGES_DIR",
        "WP_CLI_REQUIRE",
        "DB_PASSWORD",
        "X_SECRET",
    }
    sentinels = {}
    for name in sorted(planted):
        sentinels[name] = f"sentinel-{name.lower()}-{uuid.uuid4().hex}"
        monkeypatch.setenv(name, sentinels[name])
    fake = _one_pair_wp()
    _relocate(fake, site_root)
    assert fake.calls
    expected_keys = {*WP_CLI_ENV_KEYS, "WP_CLI_CONFIG_PATH"}
    for call in fake.calls:
        assert set(call.env) == expected_keys
        assert call.env["LC_ALL"] == "C"
        assert call.env["PATH"] == os.environ["PATH"]
        blob = "\0".join([*call.env.values(), *call.argv])
        for name, value in sentinels.items():
            assert value not in blob, f"{name} reached a wp child"


def test_the_real_runner_passes_exactly_the_built_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: ``run_child`` itself, with a real child, not a recording double."""
    sentinels = {
        name: f"sentinel-{name.lower()}-{uuid.uuid4().hex}"
        for name in sorted({*_credential_variables(), "MYSQL_PWD", "X_SECRET"})
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    config = tmp_path / "config.yml"
    config.touch()
    env = wpcli.wp_cli_environment(config)
    dump = "import os, json; print(json.dumps(dict(os.environ)))"
    result = wpcli.run_child([sys.executable, "-I", "-c", dump], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert set(seen) == {*WP_CLI_ENV_KEYS, "WP_CLI_CONFIG_PATH"}
    for name, value in sentinels.items():
        assert value not in result.stdout, f"{name} reached the child"


# --------------------------------------------------------------------------
# Review fixes: interference (H1), post-write output (H2), symlinks (M1), L2
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize(
    ("stored", "urls"),
    [
        (  # a ccTLD move: pair 2 would rewrite pair 1's .com.au to .com.au.au
            ("https://example.com/wp", "https://example.com"),
            ("https://example.com.au/wp", "https://example.com.au"),
        ),
        (  # only home changes host: siteurl links would end on b.test/blog
            ("https://a.test/wp", "https://a.test"),
            ("https://a.test/blog", "https://b.test"),
        ),
    ],
    ids=["cctld", "home-moves-host"],
)
def test_interfering_pairs_are_refused_before_any_preview(
    site_root: Path, stored: tuple[str, str], urls: tuple[str, str]
) -> None:
    fake = FakeWp(siteurl=stored[0], home=stored[1])
    with pytest.raises(InterferingReplacementsError) as caught:
        _relocate(fake, site_root, UrlRelocation(*urls))
    assert isinstance(caught.value, RefusedError)
    assert "search-replace" not in " ".join(" ".join(c.argv) for c in fake.calls)


@pytest.mark.usefixtures("not_root")
def test_pairs_are_reordered_when_only_another_order_is_safe(site_root: Path) -> None:
    old_site, old_home = "https://a.test/wp", "https://b.test"
    new_site, new_home = "https://b.test/wp", "https://c.test"
    fake = FakeWp(
        siteurl=old_site,
        home=old_home,
        previews={old_site: _table([]), old_home: _table([])},
    )
    report = _relocate(fake, site_root, UrlRelocation(new_site, new_home))
    assert report.pairs == (
        Replacement(old_home, new_home),
        Replacement(old_site, new_site),
    )
    olds = [
        c.argv[c.argv.index("search-replace") + 1]
        for c in fake.calls
        if "search-replace" in c.argv
    ]
    assert olds[:2] == [old_home, old_site]


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize("step", ["replace", "recount"])
def test_unrecognised_output_after_a_write_says_it_may_be_made(
    site_root: Path, step: str
) -> None:
    class Noisy(FakeWp):
        def _stdout(self, argv: list[str]) -> str:
            out = super()._stdout(argv)
            is_count = "--format=count" in argv
            hit = is_count and (("--dry-run" in argv) is (step == "recount"))
            return f"Notice: x\n{out}" if hit else out

    fake = Noisy(previews={OLD: _table([("wp_options", "v", 3)])}, replaced={OLD: 3})
    with pytest.raises(WpCliOutputUnrecognisedError) as caught:
        _relocate(fake, site_root)
    assert caught.value.step == step
    assert caught.value.replaced_maybe is True
    assert "nothing was replaced" not in str(caught.value)
    assert "may have been made" in str(caught.value)


@pytest.mark.usefixtures("not_root")
def test_a_wp_cli_config_above_a_symlinked_state_dir_is_refused(
    site_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "physical" / "inner" / "state"
    real.mkdir(parents=True)
    (tmp_path / "physical" / "wp-cli.yml").write_text("require: [m.php]\n", "utf-8")
    link = tmp_path / "lexical" / "state"
    link.parent.mkdir()
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(wpcli, "journal_directory", lambda _root: link)
    fake = _one_pair_wp()
    with pytest.raises(WpCliConfigFoundError) as caught:
        _relocate(fake, site_root)
    assert caught.value.path == tmp_path / "physical" / "wp-cli.yml"
    assert fake.calls == []


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize(
    ("urls", "warned"),
    [
        (UrlRelocation(NEW, None), True),
        (UrlRelocation(None, NEW), True),
        (UrlRelocation(NEW, NEW), False),
    ],
)
def test_one_url_over_equal_stored_values_warns_home_moved(
    site_root: Path, urls: UrlRelocation, warned: bool
) -> None:
    with capture_logs() as logs:
        _relocate(_one_pair_wp(), site_root, urls)
    moved = [e for e in logs if e["event"] == "search_replace_home_moved"]
    assert bool(moved) is warned


# --------------------------------------------------------------------------
# Epic closure (E007): stored URLs are untrusted; the child is bounded and
# cannot read its parent's /proc entries
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("not_root")
@pytest.mark.parametrize("option", ["siteurl", "home"])
@pytest.mark.parametrize(
    "value",
    [
        "--require=/tmp/x.php",
        "--export=/tmp/out.sql",
        "ftp://old.example.com",
        "http://old.example.com/\x1b]0;x\x07",
        "http://old.example.com/a b",
        'http://old.example.com/"',
    ],
    ids=["require", "export", "scheme", "control", "space", "quote"],
)
def test_a_stored_url_that_is_not_a_url_is_refused_before_any_wp_replace(
    site_root: Path, option: str, value: str
) -> None:
    """RT-5/AR-4: the Archive's own value never becomes a ``wp`` argument."""
    fake = FakeWp(**{option: value})  # type: ignore[arg-type]
    with pytest.raises(StoredUrlInvalidError) as caught:
        _relocate(fake, site_root)
    assert isinstance(caught.value, RefusedError)
    assert caught.value.option == option
    assert value not in str(caught.value)
    sent = "\0".join("\0".join(c.argv) for c in fake.calls)
    assert value not in sent
    assert "preview" not in fake.steps()
    assert "replace" not in fake.steps()


@pytest.mark.usefixtures("not_root")
def test_a_stored_url_with_a_trailing_slash_is_still_replaced(site_root: Path) -> None:
    fake = _one_pair_wp()
    fake.siteurl = fake.home = f"{OLD}/"
    _relocate(fake, site_root)
    assert "replace" in fake.steps()


@pytest.mark.usefixtures("not_root")
def test_a_wp_child_that_times_out_is_a_failed_call_not_a_hang(
    site_root: Path,
) -> None:
    """RT-2/AR-3: ``TimeoutExpired`` becomes ``WpCliFailedError``, step named."""

    def hang(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    replacer = WpCliSearchReplace(WP, secrets=SecretRegistry(), run=hang)
    with pytest.raises(WpCliFailedError) as caught:
        replacer.relocate_content(site_root, UrlRelocation(NEW, NEW))
    assert caught.value.step == "version"
    assert "timed out" in caught.value.stderr_tail


def test_the_real_runner_kills_a_child_that_outlives_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wpcli, "CHILD_TIMEOUT_S", 0.5)
    code = "import time; time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        wpcli.run_child(
            [sys.executable, "-I", "-c", code],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
        )


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() == 0,
    reason="/proc/<pid>/environ is guarded by the dumpable flag for a non-root user",
)
def test_the_child_cannot_read_the_parents_proc_environ(tmp_path: Path) -> None:
    """RT-1: same-user restored PHP could read the passphrase from /proc/<ppid>."""
    code = (
        "import os, sys\n"
        "try:\n"
        "    open(f'/proc/{os.getppid()}/environ', 'rb').read(1)\n"
        "except PermissionError:\n"
        "    sys.exit(7)\n"
    )
    argv = [sys.executable, "-I", "-c", code]
    env = {"PATH": os.environ["PATH"]}
    # Control: without the guard the read succeeds, so the test can tell.
    plain = subprocess.run(argv, cwd=tmp_path, env=env, check=False)  # noqa: S603
    assert plain.returncode == 0
    guarded = wpcli.run_child(argv, cwd=tmp_path, env=env)
    assert guarded.returncode == 7
    # The flag is put back afterwards.
    again = subprocess.run(argv, cwd=tmp_path, env=env, check=False)  # noqa: S603
    assert again.returncode == 0
