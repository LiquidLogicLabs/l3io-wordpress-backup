"""AD-16 — argument-free drivers shared by the CLI and Epic 6's ``api/``.

``run_backup``/``run_verify`` make one driver call and open no Run themselves;
no function below them receives ``args``; the drivers never print; a driver
given a Run joins it and binds nothing; one that opened the Run binds once,
after the Site Descriptor is built.

The AST checks' scope is every ``cli/*.py`` module that defines a ``run_*``
root, derived from the source and asserted to hold both roots.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import verify as verify_cli
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import InvalidSiteDescriptorError
from l3io.wp.backup.ports.adapter_set import AdapterSet

pytestmark = pytest.mark.usefixtures("fake_database")

CLI = Path(backup_cli.__file__).parent
OPENERS = frozenset({"open_run", "join_or_open"})


def _request(site: Path, final: Path) -> backup_cli.BackupRequest:
    return backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )


# --------------------------------------------------------------------------
# The roots, read from their source
# --------------------------------------------------------------------------


def _roots(directory: Path) -> dict[Path, list[ast.FunctionDef]]:
    found: dict[Path, list[ast.FunctionDef]] = {}
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        if any(f.name.startswith("run_") for f in functions):
            found[path] = functions
    return found


def _opening_roots(directory: Path) -> list[str]:
    return sorted(
        f"{path.relative_to(directory)}:{function.name}"
        for path, functions in _roots(directory).items()
        for function in functions
        if function.name.startswith("run_")
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
        in OPENERS
    )


def _takes_args(function: ast.FunctionDef) -> bool:
    """A parameter named ``args`` or annotated ``argparse.Namespace``."""
    params = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    return any(
        p.arg == "args"
        or (p.annotation is not None and "Namespace" in ast.unparse(p.annotation))
        for p in params
    )


def _args_below_roots(directory: Path) -> list[str]:
    """Every function but a ``run_*`` root, in every module, that takes ``args``."""
    return sorted(
        f"{path.relative_to(directory)}:{node.name}"
        for path in sorted(directory.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("run_")
        and _takes_args(node)
    )


def test_the_scope_holds_both_roots() -> None:
    roots = {f.name for functions in _roots(CLI).values() for f in functions} & {
        "run_backup",
        "run_verify",
    }
    assert roots == {"run_backup", "run_verify"}


def test_no_root_opens_a_run_itself() -> None:
    assert _opening_roots(CLI) == []


def test_no_function_below_a_root_receives_args() -> None:
    """Scope: the whole package, not only the modules that define a root."""
    package = CLI.parent
    assert len(list(package.rglob("*.py"))) > 10
    assert _args_below_roots(package) == []


def test_the_ast_checks_fire_on_a_planted_root(tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "x.py").write_text(
        "def run_x(args):\n"
        "    with ctx.join_or_open(None, operation='x'):\n        pass\n"
        "def helper(args):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "y.py").write_text(
        "def helper(ns: argparse.Namespace):\n    pass\ndef fine(request):\n    pass\n"
    )
    assert _opening_roots(tmp_path) == ["deep/x.py:run_x"]
    assert _args_below_roots(tmp_path) == ["deep/x.py:helper", "y.py:helper"]


# --------------------------------------------------------------------------
# One driver call per root
# --------------------------------------------------------------------------


@pytest.fixture
def driver_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real_backup, real_verify = backup_cli.drive_backup, verify_cli.drive_verify

    def backup_spy(*a: object, **kw: object) -> object:
        calls.append("drive_backup")
        return real_backup(*a, **kw)  # type: ignore[arg-type]

    def verify_spy(*a: object, **kw: object) -> object:
        calls.append("drive_verify")
        return real_verify(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_cli, "drive_backup", backup_spy)
    monkeypatch.setattr(verify_cli, "drive_verify", verify_spy)
    return calls


def test_each_root_calls_its_driver_once(
    site: Path, tmp_path: Path, driver_calls: list[str]
) -> None:
    final = tmp_path / "out.tar.gz"
    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) == 0
    assert driver_calls == ["drive_backup"]
    assert main(["verify", "--archive", str(final)]) == 0
    assert driver_calls == ["drive_backup", "drive_verify"]


# --------------------------------------------------------------------------
# The drivers
# --------------------------------------------------------------------------


def test_no_driver_writes_to_stdout(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Logging captured (an unconfigured structlog prints to stdout by default,
    which is the host's configuration, not the driver's output)."""
    final = tmp_path / "out.tar.gz"
    with capture_logs():
        result = backup_cli.drive_backup(_request(site, final), ctx=None)
        verified = verify_cli.drive_verify(
            verify_cli.VerifyRequest(
                archive=backup_cli.storage_target_from_cli(str(final))
            ),
            ctx=None,
        )
    assert result.performed
    assert verified.passed
    assert capsys.readouterr().out == ""


def test_a_driver_given_a_run_joins_it_and_binds_nothing(
    site: Path, tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with capture_logs() as logs, open_run(operation="restore") as ctx:
        bound = real_adapters(
            wordpress_source=object(),  # type: ignore[arg-type]
            secrets=ctx.secrets,
            tracer=ctx.tracer,
        )
        ctx.bind_adapters(bound)
        binds: list[object] = []
        monkeypatch.setattr(
            RunContext, "bind_adapters", lambda self, s: binds.append(s)
        )
        final = tmp_path / "joined.tar.gz"
        backup_cli.drive_backup(_request(site, final), ctx=ctx)
        verify_cli.drive_verify(
            verify_cli.VerifyRequest(
                archive=backup_cli.storage_target_from_cli(str(final))
            ),
            ctx=ctx,
        )
        assert ctx.adapters is bound
    assert binds == []
    assert len([e for e in logs if e["event"] == "run_started"]) == 1


def test_a_driver_that_opened_the_run_binds_once_after_the_descriptor(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_bind = RunContext.bind_adapters
    real_descriptor = backup_cli.site_descriptor_from_wp_config

    def bind(self: RunContext, adapter_set: AdapterSet) -> None:
        order.append("bind_adapters")
        real_bind(self, adapter_set)

    def descriptor(site_root: Path, *, config_file: Path, **kwargs: object) -> object:
        order.append("site_descriptor")
        return real_descriptor(site_root, config_file=config_file, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RunContext, "bind_adapters", bind)
    monkeypatch.setattr(backup_cli, "site_descriptor_from_wp_config", descriptor)
    backup_cli.drive_backup(_request(site, tmp_path / "a.tar.gz"), ctx=None)
    assert order == ["site_descriptor", "bind_adapters"]


def test_drive_verify_binds_nothing(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "a.tar.gz"
    backup_cli.drive_backup(_request(site, final), ctx=None)
    binds: list[object] = []
    monkeypatch.setattr(RunContext, "bind_adapters", lambda self, s: binds.append(s))
    request = verify_cli.VerifyRequest(
        archive=backup_cli.storage_target_from_cli(str(final))
    )
    assert verify_cli.drive_verify(request, ctx=None).passed
    assert binds == []


def test_a_missing_site_root_is_refused_before_any_run(
    tmp_path: Path, workdir: Path
) -> None:
    request = _request(tmp_path / "missing", tmp_path / "a.tar.gz")
    with capture_logs() as logs, pytest.raises(InvalidSiteDescriptorError) as caught:
        backup_cli.drive_backup(request, ctx=None)
    assert str(tmp_path / "missing") in str(caught.value)
    assert [e for e in logs if e["event"] == "run_started"] == []
    assert list(workdir.iterdir()) == []
