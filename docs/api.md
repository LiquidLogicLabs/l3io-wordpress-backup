# The Python API

`l3io-wordpress-backup` can be driven from Python as well as from `wpbackup2`.
This page lists every name a caller may rely on. Everything else in the
distribution is internal.

## What is public

A name is public if and only if it is listed in one of two `__all__` lists:

- `l3io.wp.backup.__all__`, which holds only `__version__`;
- `l3io.wp.backup.api.__all__`, which holds everything else.

```sh
python -c "import l3io.wp.backup.api as a; print(a.__all__)"
```

Import public names from `l3io.wp.backup.api`. The module a class happens to
be defined in, for example `l3io.wp.backup.domain.site`, is not part of the
contract and may move in any release.

**Everything else is internal, by layer, whether or not its name starts with
an underscore.** Every module under `l3io.wp.backup.domain`, `.ports`,
`.core`, `.adapters` or `.cli` is internal. So are `api/operations.py` and
`api/errors.py`: their public names are the ones `api/__init__.py`
re-exports. Importing an internal module works, but carries no compatibility
promise, and it can bypass the package's own safeguards (the redaction chain,
the secret registry), which is why the adapters are not offered as API.

The CLI's own contract (command names, options, exit codes and the log record
schema) is separate, frozen by AD-22, and described in `wpbackup2 --help`,
[`logging.md`](logging.md) and [`restore.md`](restore.md). It is not part of
this surface.

**Before v1.0 the Python API is unstable.** Any release before v1.0 may
rename, remove or change a name on this page. The surface is declared now so
that a compatibility promise can begin at v1.0; from then on, a change to a
listed name is a versioning event under the deprecation policy.

## Before you call it

**Logging.** The package never configures logging when called as a library.
A host that wants the package's renderer and redaction calls
`configure_logging` itself. A host that configures logging itself gets no
`redact_event`, so its own chain is responsible for keeping secrets out of
its records. A host that wants trace ids in its records adds `add_trace_ids`
to its own chain, or calls
`configure_logging(level, fmt, trace_processors=(add_trace_ids,))`. The record
schema, the `run_id` correlation and binding your own `correlation_id` are in
[`logging.md`](logging.md).

A host that configures nothing gets structlog's defaults: every record the
package emits, at every level including `DEBUG`, is printed to **stdout**, with
no redaction step. If stdout is a data channel for your process, call
`configure_logging` or configure structlog before the first call.

**Joining a Run.** `context=` is for a caller that is already inside a Run.
v0 offers no public way to open one, so a library caller normally omits it,
and each call opens its own Run. Calling an entry point while a Run is active
in the current context without passing its `context` raises `RunContextActiveError`.

**The environment.** A library call may read these variables from the host's
environment. The first three rows are the package's own; the rest are
standard process variables it uses.

| Variable | Read when |
|---|---|
| `L3IO_WP_BACKUP_PASSPHRASE` | the call resolves a passphrase: `backup(encrypt=True)`, `verify(authenticate=True)`, every `restore`. The `passphrase` argument is tried first and `prompt` last. A backup without `encrypt=True` only notes that it is set. |
| `L3IO_WP_BACKUP_DB_USER`, `L3IO_WP_BACKUP_DB_PASSWORD` | the WordPress database account is resolved and no `credentials=` spec was given. |
| `L3IO_WP_BACKUP_ADMIN_USER`, `L3IO_WP_BACKUP_ADMIN_PASSWORD` | a restore resolves the database administrator account and no `admin=` spec was given. |
| `PATH` | the package locates an external tool (`wp` and the readiness probes). Only absolute entries are searched. `PATH` is also passed to the WP-CLI and probe child processes. |
| `HOME` | passed to the WP-CLI and probe child processes, which use it for their own cache and configuration. No other variable of yours reaches them. |
| `TMPDIR`, `TEMP`, `TMP` | the Run's private workspace is created, through `tempfile`. The workspace holds the database dump while a Run is in progress. |

An `AwsSecretCredentialSpec` reaches AWS Secrets Manager through the AWS SDK,
which reads the standard `AWS_*` variables and configuration files.
`l3io-wordpress-database`, `l3io-wordpress-config` and an OpenTelemetry SDK
the host configures read their own variables; those are documented by each.

**What the Run reports.** When a non-package exception escapes an entry point,
the caller receives `UnexpectedError`, with the original on `__cause__`. If
the exception escaped a Run the call opened, that Run's `run_finished.error`
record and its span's `error.type` still name the original exception's type,
because they are written as the exception leaves the Run, before the boundary
translates it. Two cases differ. A failure before the Run opens (a `prompt`
callable that raises, for example) writes no `run_finished`; only the
boundary's `unexpected_exception` record names it. A call that joined an
enclosing Run through `context=` writes no `run_finished` of its own, and the
enclosing Run, if it records the failure, sees `UnexpectedError`.

**Types you meet but do not import.** Some fields and parameters below are
typed with internal classes, for example `Integrity`, `BackupPlan`,
`RestorePhase` or `SecretRegistry`. Those classes are not public. Read such a
field's value; do not import its type from the module that defines it. Where
the type is a string enumeration, its string values are what to compare
against.

## Package root

### `__version__`

The installed distribution's version, read from its metadata. Also available
as `l3io.wp.backup.__version__`, which is the only name the package root
exports.

## Entry points

### `backup`

Back up the Site whose root is `site`; returns what was published.

`scope` has no default (AD-5). `target=None` is the CLI's default
Archive name in the current directory. `exclusion=None` is the set the
CLI applies with no exclusion flag. `encrypt=True` requires a passphrase
(`passphrase`, `L3IO_WP_BACKUP_PASSPHRASE`, then `prompt`) and
refuses with `PassphraseUnavailableError` before the Site is read; a
`passphrase` without `encrypt=True` is refused (`UsageError`) rather
than ignored, so an Archive is never written plain by omission, and a
`L3IO_WP_BACKUP_PASSPHRASE` set without it is logged as the CLI logs it
(`passphrase_ignored`).
`dry_run=True` plans, checks and writes nothing.

```python
backup(site: str | os.PathLike[str], *, scope: BackupScope, target: StorageTarget | str | os.PathLike[str] | None = None, wp_config: str | os.PathLike[str] | None = None, credentials: CredentialSpec | None = None, exclusion: ExclusionPolicy | None = None, encrypt: bool = False, passphrase: str | None = None, prompt: Callable[[str], str] | None = None, dry_run: bool = False, context: RunContext | None = None) -> BackupResult
```

| Parameter | Type | Default |
|---|---|---|
| `site` | `str \| os.PathLike[str]` | required |
| `scope` | `BackupScope` | required |
| `target` | `StorageTarget \| str \| os.PathLike[str] \| None` | `None` |
| `wp_config` | `str \| os.PathLike[str] \| None` | `None` |
| `credentials` | `CredentialSpec \| None` | `None` |
| `exclusion` | `ExclusionPolicy \| None` | `None` |
| `encrypt` | `bool` | `False` |
| `passphrase` | `str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `dry_run` | `bool` | `False` |
| `context` | `RunContext \| None` | `None` |

Returns: `BackupResult`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`.

### `verify`

Check `archive` without touching a Site; returns the passing result.

Keyless by default: a pass proves the checksums are consistent, not that
the Archive is authentic (ADR-0024). `authenticate=True` resolves the
passphrase and decrypts the payload, as `verify --authenticate` does.
A failed verification raises `VerificationFailedError` carrying the
result, as the CLI exits `VERIFICATION_FAILED`.

```python
verify(archive: StorageTarget | str | os.PathLike[str], *, authenticate: bool = False, passphrase: str | None = None, prompt: Callable[[str], str] | None = None, context: RunContext | None = None) -> VerificationResult
```

| Parameter | Type | Default |
|---|---|---|
| `archive` | `StorageTarget \| str \| os.PathLike[str]` | required |
| `authenticate` | `bool` | `False` |
| `passphrase` | `str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `context` | `RunContext \| None` | `None` |

Returns: `VerificationResult`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`.

### `restore`

Restore `archive` into the Site at `site_root`; returns what was done.

`mode`, `overrides`, `snapshot` and `config_policy` have no
defaults (AD-5); FR-7's guard runs whichever root called. The passphrase
is resolved on every call: it decrypts an encrypted Archive and encrypts
the Safety Snapshot. `admin_tls=None` is the policy default
(`AdminTls(mode=None, ca_file=None)`), `relocation=None` no
relocation. A real restore whose database report does not say restored
raises `DatabaseNotRestoredError`, as the CLI exits `FAILED`.

```python
restore(site_root: str | os.PathLike[str], *, archive: StorageTarget | str | os.PathLike[str], mode: RestoreMode, overrides: OverrideSet, snapshot: SnapshotPolicy, config_policy: ConfigPolicy, snapshot_dir: str | os.PathLike[str] | None = None, admin: CredentialSpec | None = None, admin_tls: AdminTls | None = None, archive_database: str | None = None, reset_existing_db_user: bool = False, relocation: RelocationFlags | None = None, require_encrypted: bool = False, passphrase: str | None = None, prompt: Callable[[str], str] | None = None, resolve_run_id: str | None = None, dry_run: bool = False, context: RunContext | None = None) -> RestoreOutcome
```

| Parameter | Type | Default |
|---|---|---|
| `site_root` | `str \| os.PathLike[str]` | required |
| `archive` | `StorageTarget \| str \| os.PathLike[str]` | required |
| `mode` | `RestoreMode` | required |
| `overrides` | `OverrideSet` | required |
| `snapshot` | `SnapshotPolicy` | required |
| `config_policy` | `ConfigPolicy` | required |
| `snapshot_dir` | `str \| os.PathLike[str] \| None` | `None` |
| `admin` | `CredentialSpec \| None` | `None` |
| `admin_tls` | `AdminTls \| None` | `None` |
| `archive_database` | `str \| None` | `None` |
| `reset_existing_db_user` | `bool` | `False` |
| `relocation` | `RelocationFlags \| None` | `None` |
| `require_encrypted` | `bool` | `False` |
| `passphrase` | `str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `resolve_run_id` | `str \| None` | `None` |
| `dry_run` | `bool` | `False` |
| `context` | `RunContext \| None` | `None` |

Returns: `RestoreOutcome`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`.

### `enumerate_defaults`

The default set, as `--list-default-excludes` and `api/` print it.

```python
enumerate_defaults() -> tuple[str, ...]
```

Returns: `tuple[str, ...]`.

## Values

### `AdminTls`

The restore's TLS override for its database connections.

```python
AdminTls(mode: str | None, ca_file: Path | None)
```

| Field | Type | Default |
|---|---|---|
| `mode` | `str \| None` | required |
| `ca_file` | `Path \| None` | required |

### `AwsSecretCredentialSpec`

A pair held in AWS Secrets Manager; bound by Epic 5's provider.

```python
AwsSecretCredentialSpec(secret_id: str, region: str | None)
```

| Field | Type | Default |
|---|---|---|
| `secret_id` | `str` | required |
| `region` | `str \| None` | required |

### `BackupResult`

What a completed backup published.

```python
BackupResult(path: PurePosixPath, scope: BackupScope, bytes: int, encrypted: bool, integrity: Integrity | None, performed: bool, plan: BackupPlan, actions: tuple[str, ...] = ())
```

| Field | Type | Default |
|---|---|---|
| `path` | `PurePosixPath` | required |
| `scope` | `BackupScope` | required |
| `bytes` | `int` | required |
| `encrypted` | `bool` | required |
| `integrity` | `Integrity \| None` | required |
| `performed` | `bool` | required |
| `plan` | `BackupPlan` | required |
| `actions` | `tuple[str, ...]` | `()` |

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `BackupScope`

What a backup captures.

Members: `FILES` (`'files'`), `DATABASE` (`'database'`), `ALL` (`'all'`).

### `ConfigPolicy`

Whether a restore may replace the target's `wp-config.php` (ADR-0023).

Members: `KEEP` (`'keep'`), `OVERWRITE` (`'overwrite'`).

### `CredentialSpec`

```python
CredentialSpec = ExplicitCredentialSpec | AwsSecretCredentialSpec
```

### `ExclusionPolicy`

Decides, per Site Root path, whether it goes into the Archive.

Members: `source`, `patterns`, `decide`.

### `ExclusionSet`

An ordered gitignore pattern list; later patterns win, `!` re-includes.

```python
ExclusionSet(patterns: tuple[str, ...], source: ExclusionSource)
```

| Field | Type | Default |
|---|---|---|
| `patterns` | `tuple[str, ...]` | required |
| `source` | `ExclusionSource` | required |

Build one with a factory rather than the constructor:

- `ExclusionSet.default()`: the default set, as `enumerate_defaults()` lists it.
- `ExclusionSet.none()`: excludes nothing.
- `ExclusionSet.from_patterns(patterns, *, include_defaults)`: the defaults (if
  `include_defaults`) first, then `patterns`, so a later `!pattern` can
  re-include a default.

### `ExplicitCredentialSpec`

A username and password given directly. `repr` omits the password.

```python
ExplicitCredentialSpec(username: str, password: str)
```

| Field | Type | Default |
|---|---|---|
| `username` | `str` | required |
| `password` | `str` | required |

### `Override`

An FR-7 refusal that `--force` can waive.

Members: `NONEMPTY_FILES` (`'nonempty-files'`), `NONEMPTY_DATABASE` (`'nonempty-database'`).

### `OverrideSet`

The FR-7 refusals waived for a Run — never a Mode, never a removal.

```python
OverrideSet(waived: frozenset[Override])
```

| Field | Type | Default |
|---|---|---|
| `waived` | `frozenset[Override]` | required |

`OverrideSet.none()` waives nothing.

### `RelocationFlags`

The relocation flags, copied verbatim; Epic 7 validates them.

```python
RelocationFlags(*, new_site_url: str | None, new_home_url: str | None, new_db_host: str | None, new_db_name: str | None, new_db_user: str | None, new_db_password: str | None, wp_config: Path | None)
```

| Field | Type | Default |
|---|---|---|
| `new_site_url` | `str \| None` | required |
| `new_home_url` | `str \| None` | required |
| `new_db_host` | `str \| None` | required |
| `new_db_name` | `str \| None` | required |
| `new_db_user` | `str \| None` | required |
| `new_db_password` | `str \| None` | required |
| `wp_config` | `Path \| None` | required |

`RelocationFlags.none()` gives no relocation flag; `restore(relocation=None)` means the same.

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `RestoreMode`

FR-6's Restore Modes. Resolved once from the flags, carried as a value.

Members: `FILES` (`'files'`), `FILES_CLEAN` (`'files-clean'`), `DATABASE` (`'database'`), `DATABASE_CLEAN` (`'database-clean'`), `ALL` (`'all'`), `ALL_CLEAN` (`'all-clean'`).

### `RestoreOutcome`

What one restore did (or, in a Dry Run, planned); built, never printed.

```python
RestoreOutcome(*, run_id: str, mode: RestoreMode, phases: tuple[RestorePhase, ...], dry_run: bool, actions: tuple[PlannedRestoreAction, ...], integrity: Integrity | None, snapshot_path: PurePosixPath | None, snapshot_encrypted: bool, database: DatabaseRestoreReport | None, search_replace: SearchReplaceReport | None, resolved_run_id: str | None, skipped_links: tuple[SkippedLink, ...], wp_config_kept: bool)
```

| Field | Type | Default |
|---|---|---|
| `run_id` | `str` | required |
| `mode` | `RestoreMode` | required |
| `phases` | `tuple[RestorePhase, ...]` | required |
| `dry_run` | `bool` | required |
| `actions` | `tuple[PlannedRestoreAction, ...]` | required |
| `integrity` | `Integrity \| None` | required |
| `snapshot_path` | `PurePosixPath \| None` | required |
| `snapshot_encrypted` | `bool` | required |
| `database` | `DatabaseRestoreReport \| None` | required |
| `search_replace` | `SearchReplaceReport \| None` | required |
| `resolved_run_id` | `str \| None` | required |
| `skipped_links` | `tuple[SkippedLink, ...]` | required |
| `wp_config_kept` | `bool` | required |

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `RunContext`

One Run: its identity, logger, clock, registry, tracer and workspace.

```python
RunContext(run_id: str, started_at: datetime, workspace: Path, logger: structlog.stdlib.BoundLogger, clock: Clock, secrets: SecretRegistry, tracer: Tracer, run_span: RunSpan, dry_run: bool = False, site_lock: LockHandle | None = None)
```

| Field | Type | Default |
|---|---|---|
| `run_id` | `str` | required |
| `started_at` | `datetime` | required |
| `workspace` | `Path` | required |
| `logger` | `structlog.stdlib.BoundLogger` | required |
| `clock` | `Clock` | required |
| `secrets` | `SecretRegistry` | required |
| `tracer` | `Tracer` | required |
| `run_span` | `RunSpan` | required |
| `dry_run` | `bool` | `False` |
| `site_lock` | `LockHandle \| None` | `None` |

Do not construct a `RunContext`. A caller only ever holds one handed to it by an enclosing Run, and passes it on as `context=` (see *Joining a Run* above).

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `SiteDescriptor`

Everything the Run knows about one Site, validated at construction.

```python
SiteDescriptor(site_root: PurePosixPath, db_host: str, db_port: int, db_name: str, table_prefix: str, site_url: str | None, home_url: str | None, credential_source: str)
```

| Field | Type | Default |
|---|---|---|
| `site_root` | `PurePosixPath` | required |
| `db_host` | `str` | required |
| `db_port` | `int` | required |
| `db_name` | `str` | required |
| `table_prefix` | `str` | required |
| `site_url` | `str \| None` | required |
| `home_url` | `str \| None` | required |
| `credential_source` | `str` | required |

### `SnapshotPolicy`

Whether a restore takes the Safety Snapshot (`--no-safety-snapshot`).

Members: `TAKE` (`'take'`), `SKIP` (`'skip'`).

### `StorageTarget`

Where an Archive is written.

```python
StorageTarget(scheme: Literal['file'], path: PurePosixPath)
```

| Field | Type | Default |
|---|---|---|
| `scheme` | `Literal['file']` | required |
| `path` | `PurePosixPath` | required |

### `VerificationResult`

Pass or fail, and what a pass proved. `failures` is empty on a pass.

```python
VerificationResult(passed: bool, integrity: Integrity, format_version: int | None, failures: tuple[VerificationFailure, ...])
```

| Field | Type | Default |
|---|---|---|
| `passed` | `bool` | required |
| `integrity` | `Integrity` | required |
| `format_version` | `int \| None` | required |
| `failures` | `tuple[VerificationFailure, ...]` | required |

## Logging and tracing

### `configure_logging`

Configure structlog and the root handler; a second call replaces both.

```python
configure_logging(level: str, fmt: Literal['console', 'json'] | None, *, trace_processors: Sequence[Processor] = ()) -> None
```

| Parameter | Type | Default |
|---|---|---|
| `level` | `str` | required |
| `fmt` | `Literal['console', 'json'] \| None` | required |
| `trace_processors` | `Sequence[Processor]` | `()` |

Returns: `None`.

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `add_trace_ids`

Flat `trace_id` (32 hex) and `span_id` (16 hex) while a span records.

A non-recording span — no SDK, or a sampler that dropped the Run — adds
no key at all.

```python
add_trace_ids(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict
```

| Parameter | Type | Default |
|---|---|---|
| `logger` | `WrappedLogger` | required |
| `method_name` | `str` | required |
| `event_dict` | `EventDict` | required |

Returns: `EventDict`.

## Exceptions

Every class below is a `BackupError`, so `except BackupError` catches them all. Each carries `exit_code` (the exit code the CLI ends a Run with for the same failure) and `snapshot_path` (the Safety Snapshot's path, or `None`), plus the typed fields listed. No field holds a credential, passphrase or secret value.

### `BackupError`

The base of every error this package raises across a public boundary.

Base: `Exception`. Exit code: `FAILED` (1).

### `RefusedError`

A check found a reason not to act, so nothing was done.

Base: `BackupError`. Exit code: `REFUSED` (3).

### `UnexpectedError`

A non-package exception reached the API boundary. It is a defect.

```python
UnexpectedError(cause_type: str, *, api_function: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `cause_type`, `api_function`, each holding the argument of the same name.

### `AdaptersAlreadyBoundError`

ADR-0016 — a Run's adapters were bound a second time. A defect.

```python
AdaptersAlreadyBoundError(run_id: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `run_id`, each holding the argument of the same name.

### `AdaptersNotBoundError`

ADR-0016 — a Run's adapters were read before they were bound. A defect.

```python
AdaptersNotBoundError(run_id: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `run_id`, each holding the argument of the same name.

### `AdminCredentialsRequiredError`

The operation needs administrator credentials that were not given.

```python
AdminCredentialsRequiredError(source: str, *, flags: tuple[str, ...])
```

Base: `CredentialsError`. Exit code: `CREDENTIALS` (4).

Fields: `source`, `flags`, each holding the argument of the same name.

### `ArchiveChangedError`

The Archive verified now is not the one the restore was planned from.

```python
ArchiveChangedError(archive: str)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `archive`, each holding the argument of the same name.

### `ArchiveExistsError`

The Archive's final name already exists at its destination.

```python
ArchiveExistsError(path: PurePath)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, each holding the argument of the same name.

### `ArchiveHasNoConfigError`

`--overwrite-wp-config` and the Archive carries no `wp-config.php`.

```python
ArchiveHasNoConfigError(archive: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `archive`, each holding the argument of the same name.

### `ArchiveHasNoDumpError`

A Restore Mode names the database and the Archive holds no Dump.

```python
ArchiveHasNoDumpError(archive: str, backup_scope: str | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `archive`, `backup_scope`, each holding the argument of the same name.

### `ArchiveIdentityUnconfirmedError`

The target has no `wp-config.php` and `--archive-database` did not name the Archive config's database (RT-1).

```python
ArchiveIdentityUnconfirmedError(*, user: str, location: DatabaseLocation, differs: tuple[str, ...] | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `user`, `location`, `differs`, each holding the argument of the same name.

### `ArchiveNotEncryptedError`

`--authenticate` was asked of an Archive with no encrypted payload.

```python
ArchiveNotEncryptedError(archive: str, *, required: bool = False)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `archive`, `required`, each holding the argument of the same name.

### `ArchiveNotFoundError`

The Archive does not exist or cannot be opened.

```python
ArchiveNotFoundError(path: PurePath)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, each holding the argument of the same name.

### `ArchiveReadError`

An Archive could not be read to the member asked for (I/O or tar error).

```python
ArchiveReadError(path: PurePath, reason: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `reason`, each holding the argument of the same name.

### `ArchiveWriteError`

The Archive could not be written (I/O failure, `ENOSPC` included).

```python
ArchiveWriteError(path: PurePath, reason: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `reason`, each holding the argument of the same name.

### `CleanFailedError`

`clean` stopped at `entry`; the entries after it were left in place.

```python
CleanFailedError(entry: PurePath, reason: str, *, removed: int)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `entry`, `reason`, `removed`, each holding the argument of the same name.

### `ConfigCutoverError`

A cutover write failed; `key` is byte-identical, `written` landed.

```python
ConfigCutoverError(path: str, key: str, *, written: tuple[str, ...], pending: tuple[str, ...])
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `key`, `written`, `pending`, each holding the argument of the same name.

### `ConfigLocationConflictError`

FR-29: `wp-config.php` cannot be laid down where the Archive places it.

```python
ConfigLocationConflictError(path: PurePath, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `reason`, each holding the argument of the same name.

### `ConfigWriteFailedError`

A `wp-config.php` write failed. `written` names keys, never values.

```python
ConfigWriteFailedError(path: PurePath, key: str, reason: str, *, written: tuple[str, ...] = ())
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `key`, `reason`, `written`, each holding the argument of the same name.

### `CorruptJournalError`

`current.json` exists and cannot be read back (NFR-1: never absent).

```python
CorruptJournalError(journal_path: str, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `journal_path`, `reason`, each holding the argument of the same name.

### `CredentialsError`

Database credentials could not be obtained or were rejected.

```python
CredentialsError(source: str, *, missing_key: str | None = None, reason: str = '')
```

Base: `BackupError`. Exit code: `CREDENTIALS` (4).

Fields: `source`, `missing_key`, `reason`, each holding the argument of the same name.

### `CutoverTargetUnusableError`

ADV-S02-05: the database an interrupted cutover pointed at is unusable.

```python
CutoverTargetUnusableError(database: str, *, run_id: str, exists: bool)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `database`, `run_id`, `exists`, each holding the argument of the same name.

### `DatabaseAuthPluginRefusedError`

The server asked for an authentication plugin that would expose the password (cleartext, old-password or dialog). `REFUSED`.

```python
DatabaseAuthPluginRefusedError(host: str, port: int, plugin: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `host`, `port`, `plugin`, each holding the argument of the same name.

### `DatabaseNotRestoredError`

A real Run's plan loaded the Dump and the report does not say restored.

```python
DatabaseNotRestoredError(database: str | None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `database`, each holding the argument of the same name.

### `DatabaseRefusedError`

A database check found blockers; `findings` are codes only.

```python
DatabaseRefusedError(findings: tuple[str, ...])
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `findings`, each holding the argument of the same name.

### `DatabaseTlsHandshakeError`

TLS was required and the server did not negotiate it. `UNREACHABLE`.

```python
DatabaseTlsHandshakeError(host: str, port: int)
```

Base: `DatabaseUnreachableError`. Exit code: `UNREACHABLE` (5).

Fields: `host`, `port`, each holding the argument of the same name.

### `DatabaseTlsVerificationError`

The server's TLS certificate did not verify. `REFUSED`.

```python
DatabaseTlsVerificationError(host: str, port: int)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `host`, `port`, each holding the argument of the same name.

### `DatabaseUnreachableError`

The database server could not be reached.

```python
DatabaseUnreachableError(host: str, port: int)
```

Base: `BackupError`. Exit code: `UNREACHABLE` (5).

Fields: `host`, `port`, each holding the argument of the same name.

### `DumpFailedError`

The database dump started and failed.

```python
DumpFailedError(reason: str, *, database: str, exit_status: int | None = None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `database`, `exit_status`, each holding the argument of the same name.

### `EncryptionConfigError`

Database refused the passphrase or its backend setting as invalid.

```python
EncryptionConfigError(*, relative_path: bool = False)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `relative_path`, each holding the argument of the same name.

### `ExistingDatabaseUserError`

The Archive's config names a database account that already exists.

```python
ExistingDatabaseUserError(*, user: str, location: DatabaseLocation)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `user`, `location`, each holding the argument of the same name.

### `ExpansionRatioError`

The Archive expands by more than `MAX_EXPANSION_RATIO` times.

```python
ExpansionRatioError(*, expanded: int, compressed: int)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `expanded`, `compressed`, each holding the argument of the same name.

### `ExtractionRefusedError`

Extraction refused the Archive: an unsafe member, or no `data` filter.

```python
ExtractionRefusedError(member: str | None, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `member`, `reason`, each holding the argument of the same name.

### `FlagCombinationRejectedError`

AD-25 — restore flags that express no Restore Mode.

```python
FlagCombinationRejectedError(*, reason: str, use_instead: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `reason`, `use_instead`, each holding the argument of the same name.

### `IncompleteRelocationCredentialsError`

FR-11 — half of the relocated account was given.

```python
IncompleteRelocationCredentialsError(*, given: str, missing: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `given`, `missing`, each holding the argument of the same name.

### `IncompleteRunError`

AD-18: an earlier Run on this Site never finished, and nothing was resolved.

```python
IncompleteRunError(*, run_id: str, started_at: str, interrupted_phase: RestorePhase | None, undo: str, journal_path: str, written_keys: tuple[str, ...] = (), pending_keys: tuple[str, ...] = ())
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `run_id`, `started_at`, `interrupted_phase`, `undo`, `journal_path`, `written_keys`, `pending_keys`, each holding the argument of the same name.

### `InsufficientSpaceError`

A destination filesystem cannot hold what the restore writes there.

```python
InsufficientSpaceError(filesystem: str, *, needed: int, free: int, paths: tuple[str, ...] = ())
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `filesystem`, `needed`, `free`, `paths`, each holding the argument of the same name.

### `InterferingReplacementsError`

FR-12: the URL pairs would rewrite each other's output in every order.

```python
InterferingReplacementsError(pairs: tuple[tuple[str, str], ...])
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `pairs`, each holding the argument of the same name.

### `InvalidExclusionPatternError`

An exclusion pattern that cannot be used; exits `USAGE` (AD-14).

```python
InvalidExclusionPatternError(pattern: str, reason: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `pattern`, `reason`, each holding the argument of the same name.

### `InvalidLoadDatabaseNameError`

The load database's name is not a usable database identifier.

```python
InvalidLoadDatabaseNameError(name: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `name`, each holding the argument of the same name.

### `InvalidRelocationValueError`

FR-11 — a relocation flag's value breaks a rule.

```python
InvalidRelocationValueError(flag: str, rule: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `flag`, `rule`, each holding the argument of the same name.

### `InvalidSiteDescriptorError`

The Site Descriptor could not be built from what was given.

```python
InvalidSiteDescriptorError(reason: str, *, paths: tuple[str, ...] = ())
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `paths`, each holding the argument of the same name.

### `JournalWriteError`

A journal record could not be made durable; the phase does not begin.

```python
JournalWriteError(path: Path, reason: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `reason`, each holding the argument of the same name.

### `LoadFailedError`

Loading a Dump started and failed, or the Dump was unreadable.

```python
LoadFailedError(reason: str, *, database: str, exit_status: int | None = None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `database`, `exit_status`, each holding the argument of the same name.

### `MalformedManifestError`

A Manifest's JSON is not a valid v1 Manifest.

```python
MalformedManifestError(reason: str, *, field: str | None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `field`, each holding the argument of the same name.

### `MemberReadRefusedError`

A single-member read was refused: not a regular file, not indexed, too big.

```python
MemberReadRefusedError(path: str, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `reason`, each holding the argument of the same name.

### `MissingExtraError`

An optional feature was used without its extra installed (AD-8).

```python
MissingExtraError(extra: str)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `extra`, each holding the argument of the same name.

### `NoConfigForCutoverError`

A database Mode, and no `wp-config.php` will exist for the cutover.

```python
NoConfigForCutoverError(path: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, each holding the argument of the same name.

### `NonEmptyTargetError`

FR-7: a part the Mode restores is not empty, and its refusal is not waived.

```python
NonEmptyTargetError(mode: RestoreMode, *, parts: tuple[RestorePart, ...], findings: tuple[tuple[str, str], ...], authorizing: FlagCombination)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `mode`, `parts`, `findings`, `authorizing`, each holding the argument of the same name.

### `NonEmptyTargetRefusal`

FR-7's refusal, worded in the flags the operator types (BL-E003-039).

```python
NonEmptyTargetRefusal(original: NonEmptyTargetError)
```

Base: `NonEmptyTargetError`. Exit code: `REFUSED` (3).

Fields: `original`, each holding the argument of the same name.

### `PassphraseRejectedError`

The passphrase cannot open the encrypted payload (`DecryptionError`).

```python
PassphraseRejectedError(*, scheme: str)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `scheme`, each holding the argument of the same name.

### `PassphraseRequiredError`

Encryption was requested, or the Archive is encrypted, and no passphrase.

```python
PassphraseRequiredError(operation: Literal['backup', 'restore', 'verify'])
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `operation`, each holding the argument of the same name.

### `PassphraseUnavailableError`

AD-26 — passphrase encryption was requested and no usable one resolved.

```python
PassphraseUnavailableError(variable: str, *, reason: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `variable`, `reason`, each holding the argument of the same name.

### `PayloadDamagedError`

The encrypted payload is damaged, truncated or not `age`.

```python
PayloadDamagedError(field: str | None = None)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `field`, each holding the argument of the same name.

### `PreflightFailedError`

A tool the Run needs is absent or unusable.

```python
PreflightFailedError(tool: str, needed_for: str, found: str, *, install_hint: str | None = None)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `tool`, `needed_for`, `found`, `install_hint`, each holding the argument of the same name.

### `ProvisioningFailedError`

Provisioning started and failed part-way.

```python
ProvisioningFailedError(reason: str, *, completed: tuple[str, ...] = ())
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `completed`, each holding the argument of the same name.

### `RelocationNeedsDatabaseRestoreError`

FR-11 — relocation flags with a Restore Mode that restores no database.

```python
RelocationNeedsDatabaseRestoreError(*, flags: tuple[str, ...], mode: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `flags`, `mode`, each holding the argument of the same name.

### `RunContextActiveError`

AD-16 — a Run was opened while another is active in this context.

```python
RunContextActiveError(active_run_id: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `active_run_id`, each holding the argument of the same name.

### `RunTerminated`

The Run received SIGTERM; raised so its `finally` chain runs (FR-28).

```python
RunTerminated(signal_name: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `signal_name`, each holding the argument of the same name.

### `SearchReplaceIncompleteError`

FR-12: after the replacement the old URL is still found `remaining` times.

```python
SearchReplaceIncompleteError(old: str, new: str, remaining: int)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `old`, `new`, `remaining`, each holding the argument of the same name.

### `SiteLockLocationError`

The Site Lock's directory is unusable, or inside the Site Root (AD-19).

```python
SiteLockLocationError(path: PurePath, reason: str)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `path`, `reason`, each holding the argument of the same name.

### `SiteLockReleaseError`

A Site Lock handle was released more often than acquired. A defect.

```python
SiteLockReleaseError(site_root: PurePath)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `site_root`, each holding the argument of the same name.

### `SiteLockedError`

Another Run holds the Site Lock (AD-19).

```python
SiteLockedError(site_root: PurePath, lock_path: PurePath, *, holder_run_id: str | None = None, holder_pid: int | None = None, holder_started_at: str | None = None, holder_hostname: str | None = None)
```

Base: `BackupError`. Exit code: `LOCKED` (9).

Fields: `site_root`, `lock_path`, `holder_run_id`, `holder_pid`, `holder_started_at`, `holder_hostname`, each holding the argument of the same name.

### `SnapshotFailedError`

FR-8: the Safety Snapshot was not written and verified; nothing destroyed.

```python
SnapshotFailedError(path: str | None, cause: BaseException | str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `cause`, each holding the argument of the same name.

### `StagingCheckFailedError`

The loaded database is not a WordPress database; nothing was cut over.

```python
StagingCheckFailedError(database: str, *, is_wordpress: bool, schema_version: str | None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `database`, `is_wordpress`, `schema_version`, each holding the argument of the same name.

### `StoredUrlInvalidError`

FR-12: the loaded database's `siteurl` or `home` is not a URL to replace.

```python
StoredUrlInvalidError(option: str, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `option`, `reason`, each holding the argument of the same name.

### `StoredUrlMissingError`

FR-12: the loaded database has no `siteurl` or `home` value to replace.

```python
StoredUrlMissingError(option: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `option`, each holding the argument of the same name.

### `TargetChangedError`

RT-003: the target gained content the plan's snapshot does not cover.

```python
TargetChangedError(planned: BackupScope | None, found: BackupScope)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `planned`, `found`, each holding the argument of the same name.

### `TargetConfigMissingError`

The plan writes the target's `wp-config.php`, and none is there now.

```python
TargetConfigMissingError(path: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, each holding the argument of the same name.

### `UnboundAdapterError`

A port whose real implementation has not landed was called. A defect.

```python
UnboundAdapterError(port: str, *, supplier: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `port`, `supplier`, each holding the argument of the same name.

### `UnknownInterruptedRunError`

FR-28 — an acknowledgement named a Run that is not the interrupted one.

```python
UnknownInterruptedRunError(run_id: str, *, recorded_run_id: str | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `run_id`, `recorded_run_id`, each holding the argument of the same name.

### `UnreadableSiteRootError`

FR-7: the Site Root exists and cannot be listed or read (BL-E003-034).

```python
UnreadableSiteRootError(site_root: PurePath, error: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `site_root`, `error`, each holding the argument of the same name.

### `UnrecognisedArchiveError`

The Archive is none of the shapes this build reads (AD-23).

```python
UnrecognisedArchiveError(found: tuple[str, ...])
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `found`, each holding the argument of the same name.

### `UnsafeCleanTargetError`

A Site Root that `clean` refuses to empty (a mistyped `--wp-dir`).

```python
UnsafeCleanTargetError(site_root: PurePath, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `site_root`, `reason`, each holding the argument of the same name.

### `UnsafeMemberPathError`

An Archive member path would escape its role prefix.

```python
UnsafeMemberPathError(path: str, reason: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, `reason`, each holding the argument of the same name.

### `UnsafeSnapshotLocationError`

The Safety Snapshot directory is inside the Site Root or its config dir.

```python
UnsafeSnapshotLocationError(requested: PurePosixPath, inside: PurePosixPath)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `requested`, `inside`, each holding the argument of the same name.

### `UnsupportedConfigLocationError`

FR-29, AD-11: the Manifest's `config_path` is not a place WordPress reads.

```python
UnsupportedConfigLocationError(value: str, *, allowed: tuple[str, ...])
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `value`, `allowed`, each holding the argument of the same name.

### `UnsupportedFormatVersionError`

The Manifest's `format_version` is not one this build reads.

```python
UnsupportedFormatVersionError(found: int)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `found`, each holding the argument of the same name.

### `UnsupportedStorageSchemeError`

A Storage Target names a scheme this build does not support.

```python
UnsupportedStorageSchemeError(scheme: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `scheme`, each holding the argument of the same name.

### `UsageError`

The caller supplied an invalid combination of arguments.

```python
UsageError(message: str, *, arguments: tuple[str, ...])
```

Base: `BackupError`. Exit code: `USAGE` (2).

Fields: `message`, `arguments`, each holding the argument of the same name.

### `VerificationFailedError`

An Archive failed verification; `result` says why.

```python
VerificationFailedError(result: VerificationResult)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `result`, each holding the argument of the same name.

### `WpCliConfigFoundError`

AD-11: a `wp-cli.yml` WP-CLI would read sits in an ancestor of its directory.

```python
WpCliConfigFoundError(path: PurePath)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, each holding the argument of the same name.

### `WpCliFailedError`

FR-12: a `wp` call exited non-zero or could not be started.

```python
WpCliFailedError(step: str, exit_status: int | None, stderr_tail: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `step`, `exit_status`, `stderr_tail`, each holding the argument of the same name.

### `WpCliOutputUnrecognisedError`

FR-12: WP-CLI's output did not have the shape this package reads.

```python
WpCliOutputUnrecognisedError(step: str, reason: str, *, wp_cli_version: str, replaced_maybe: bool)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `step`, `reason`, `wp_cli_version`, `replaced_maybe`, each holding the argument of the same name.
