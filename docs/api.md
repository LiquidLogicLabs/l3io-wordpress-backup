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
schema, the `l3io_run_id` correlation and binding your own `correlation_id` are in
[`logging.md`](logging.md).

A host that configures nothing gets structlog's defaults: every record the
package emits, at every level including `DEBUG`, is printed to **stdout**, with
no redaction step. If stdout is a data channel for your process, call
`configure_logging` or configure structlog before the first call.

**One Run per call.** Each call opens its own Run. Calling an entry point
while a Run is active in the current context raises `RunContextActiveError`.
There is no public way to join a Run: the `context=` parameter was removed
before 1.0 (owner decision 2026-11-07), and a public `open_run()` and
the parameter return with the post-v1 library refactor (BL-E006-024).

**Pre-1.0 changes (2026-11-08, the automation-first set, owner decisions
54-65).** `backup` loses `confirm_db_host` and `allow_target_in_site_root`
(the host is logged, a target in the Site Root is written with a warning) and
gains `db_tls`; `restore` loses `allow_root` (a library restore as root is
refused: the library never switches its host's user), its `admin_tls` is
renamed `db_tls` and `AdminTls` is renamed `DbTls` (no TLS is required by
default); `allow_unencrypted` has no effect (a plain Archive restored with a
passphrase proceeds with a warning); `RestoreOutcome` gains `notes`.
`CredentialTooShortError`, `DatabaseHostUnconfirmedError` and
`TargetInsideSiteRootError` are gone; `NoArchiveForSiteError` and
`PrivilegeDropError` are new (raised by the CLI).

**Pre-1.0 changes (2026-11-08, owner decisions 67-69).** `backup` refuses a
process running as root with `RootBackupRefusedError` before the Site is
read, as `restore` refuses one with `RootRestoreRefusedError`: drop
privileges to the Site Root's owner first, or use the `wpbackup2` command,
which switches to that owner. `verify` touches no Site, takes no Site Lock
and leaves nothing behind, so it is not refused. A passphrase that decrypts
(`verify`, `restore`) and is shorter than 4 characters is accepted with
WARNING `credential_below_redaction_floor`; it was refused with
`PassphraseUnavailableError`. `RootBackupRefusedError` and
`ArchiveDirectoryNotWritableError` (raised by the CLI) are new.

**Pre-1.0 changes (2026-11-09).** `InstallationNotReadableError` (raised by
the CLI) is new: a Run started as root refuses, right after switching to the
Site Root's owner, an installation that owner cannot read.

**Pre-1.0 changes (2026-11-07).** `backup`'s `target` is now required (no
default; a call without it is a `TypeError`, raised before anything runs);
the `context` parameter is gone from `backup`, `restore` and `verify`; passwords and
passphrases accept a `Secret`; `on_warning` and `doctor` are new.

**The environment.** A library call may read these variables from the host's
environment. The first three rows are the package's own, the fourth the
Site's; the rest are standard process variables it uses.

| Variable | Read when |
|---|---|
| `L3IO_WP_BACKUP_PASSPHRASE` | the call resolves a passphrase: `backup(encrypt=True)`, `verify(authenticate=True)`, every `restore`. The `passphrase` argument is tried first and `prompt` last. A backup without `encrypt=True` only notes that it is set. |
| `L3IO_WP_BACKUP_DB_USER`, `L3IO_WP_BACKUP_DB_PASSWORD` | the WordPress database account is resolved and no `credentials=` spec was given. |
| `L3IO_WP_BACKUP_ADMIN_USER`, `L3IO_WP_BACKUP_ADMIN_PASSWORD` | a restore resolves the database administrator account and no `admin=` spec was given. |
| the variables the Site's `wp-config.php` names | its `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` or `$table_prefix` is `getenv_docker('NAME', <default>)` or `getenv('NAME')`, as the official `wordpress` image writes them: `NAME`, and for `getenv_docker` the file `NAME_FILE` names (a Docker secret), resolved as PHP would (owner decision 47). `backup`, `doctor` and `restore` resolve them; a database `restore` loads the database they name and writes no `DB_*` (owner decision 49), and only a relocation that would rewrite one is refused (`EnvironmentDrivenTargetError`). A files-only `restore` with `config_policy=ConfigPolicy.OVERWRITE` carries the definitions into the Archive's file as written (owner decision 48), and refuses an Archive's file that does not define `getenv_docker()` (`UndefinedConfigFunctionError`). |
| `PATH` | the package locates an external tool (`wp` and the readiness probes). Only absolute entries are searched. `PATH` is also passed to the WP-CLI and probe child processes. |
| `HOME` | passed to the WP-CLI and probe child processes, which use it for their own cache and configuration. No other variable of yours reaches them. |
| `TMPDIR`, `TEMP`, `TMP` | the Run's private workspace is created, through `tempfile`. The workspace holds the database dump (and a decrypted payload) while a Run is in progress. A Run killed outright (`SIGKILL`, power loss) leaves it behind; each later Run of the same OS user removes `l3io-wp-backup-*` workspaces untouched for 24 hours, logging each path (`stale_residue_removed`). Pointing `TMPDIR` at a memory-backed filesystem (tmpfs) keeps that plaintext off disk altogether. |

An `AwsSecretCredentialSpec` reaches AWS Secrets Manager through the AWS SDK,
which reads the standard `AWS_*` variables and configuration files.
`l3io-wordpress-database`, `l3io-wordpress-config` and an OpenTelemetry SDK
the host configures read their own variables; those are documented by each.
The client programs `l3io-wordpress-database` (>= 0.3.3) starts get explicit
environments, not yours: `mysql` and `mysqldump` only a `PATH` of its absolute
entries and `LC_ALL=C`; `age` (the `cli` passphrase backend), run by absolute
path, only that `PATH`, `HOME`, `LC_ALL=C` and `AGE_PASSPHRASE_FD`
(BL-E004-055).

**What the Run reports.** When a non-package exception escapes an entry point,
the caller receives `UnexpectedError`, with the original on `__cause__`. If
the exception escaped a Run the call opened, that Run's `run_finished.error`
record and its span's `error.type` still name the original exception's type,
because they are written as the exception leaves the Run, before the boundary
translates it. One case differs: a failure before the Run opens (a `prompt`
callable that raises, for example) writes no `run_finished`; only the
boundary's `unexpected_exception` record names it.

The boundary's `unexpected_exception` record carries the original's class
(`cause_type`), the entry point, the `l3io_run_id` when known, and a `traceback`
field of the frames with the original's **message withheld**: a dependency's
text is untrusted, and the Run's secret registry is already closed when the
boundary logs, so the record could not be redacted. The original, message
included, stays on `__cause__` for you to handle.

**Secrets and what holds them.** The package keeps secrets out of its own
exceptions, `repr`s, log records and spans. It cannot keep them out of
memory you can reach. Pass a `passphrase=`, `ExplicitCredentialSpec(password=...)`
or `RelocationFlags(new_db_password=...)` as a `Secret` (exported from this
package; ADR-0018 amendment of 2026-11-07): its `repr` and `str` are
`Secret('***')`, and pickling or copying it raises `TypeError`, so a captured
local, `repr` or `pickle` of a spec does not show the value. A plain `str` is
accepted and wrapped where it is read, but until then it is your own local
variable in your frames. The text `.reveal()` returns is itself a `str` local
for the moment the value is used, inside the few package modules allowed to
call it. A tool that captures locals (an error tracker's local-variable option,
rich or `dict_tracebacks(show_locals=True)` tracebacks, `pytest --showlocals`)
can record such a `str`. Prefer `prompt=` or the environment to `passphrase=`
where such a tool runs, and scrub locals in it. Span status descriptions name a
non-package exception by class only. The database dependency's own
`l3io.wp.database.*` spans, which nest under this package's phase spans, carry
a failure as `error.type=<exception class>` and status ERROR with no
description, and record no `exception` event, so neither the message nor the
stack trace is exported (`l3io-wordpress-database` >= 0.3.3, BL-E006-010).

**Process state and output.** A call does not configure logging, install a
signal handler or change the working directory, `os.environ` or the tracer
provider. It does three things to the host process that you should know:

- During a call it binds `l3io_run_id` and `l3io_dry_run` in structlog's
  context variables, and restores them afterwards. The `l3io_` prefix (owner
  decision 2026-11-07, BL-E006-019) keeps a host's own `run_id` and `dry_run`
  keys untouched.
- While a `wp` child runs during a restore, the whole process is marked
  non-dumpable (`PR_SET_DUMPABLE`, Linux), so the child cannot read this
  process's `/proc` entries. For that time (up to four hours per child) the
  process has no core dump and no same-user `ptrace`. Overlapping calls share
  one toggle, restored when the last child is done.
- `restore` writes safety notices to **stderr** (the archive is not
  authenticated; whether the Safety Snapshot is encrypted) unless you pass
  `on_warning`, which then receives each as a `RunWarning` instead. The
  WARNING log records are written either way. An exception your callback
  raises propagates out of the call, except for a notice given after the
  Safety Snapshot was taken (whether the snapshot is encrypted): that one is
  logged (ERROR `on_warning_failed`, naming the notice's code and the
  exception's class) and the restore continues, so a broken callback cannot
  leave a snapshot with no restore. The snapshot path is in
  `RestoreOutcome.snapshot_path`.

Every package error survives `pickle` and `copy` with its fields, so a worker
pool or task queue delivers the real exception. `__cause__` and the traceback
do not travel.

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

`scope` and `target` have no default (AD-5; `target` is required since
2026-11-07, and the CLI's default Archive name is the CLI's alone).
`exclusion=None` is the set the CLI applies with no exclusion flag: the
default exclusion set, logged. `encrypt=True` requires a passphrase
(`passphrase`, a `str` or `Secret`; `L3IO_WP_BACKUP_PASSPHRASE`; then
`prompt`, asked twice) of at least 12 characters (`PassphraseTooShortError`,
`PassphraseMismatchError`) and refuses with `PassphraseUnavailableError` when
none resolves, before the Site is read; a
`passphrase` without `encrypt=True` is refused (`UsageError`) rather
than ignored, so an Archive is never written plain by omission, and a
`L3IO_WP_BACKUP_PASSPHRASE` set without it is logged as the CLI logs it
(`passphrase_ignored`).
`dry_run=True` plans, checks and writes nothing. `on_warning` receives the
notices `backup` gives (`passphrase_ignored`, `archive_target_in_site_root`).
The database host is logged (INFO `database_host`), never confirmed (owner
decision 54), and a `target` inside the Site Root is written with WARNING
`archive_target_in_site_root` (owner decision 60). `db_tls` requires TLS on
the database connections, as `--db-tls`/`--db-tls-ca`; `None` requires none,
as WordPress connects (owner decision 56). A process running as root is
refused with `RootBackupRefusedError` before the Site is read: the library
never switches its host's user; drop privileges to the Site Root's owner
first, or use the `wpbackup2` command, which switches to it (owner decisions
67, 68). `compression_level` is the payload's gzip level, from 1 (fastest) to
9 (smallest), as `--compression-level`; 6 by default, where it was always 9
before 1.1.1 (owner decision 88). The Archive format is the same at every
level.

```python
backup(site: str | os.PathLike[str], *, scope: BackupScope, target: StorageTarget | str | os.PathLike[str], wp_config: str | os.PathLike[str] | None = None, credentials: CredentialSpec | None = None, exclusion: ExclusionPolicy | None = None, encrypt: bool = False, passphrase: Secret | str | None = None, prompt: Callable[[str], str] | None = None, dry_run: bool = False, on_warning: Callable[[RunWarning], None] | None = None, db_tls: DbTls | None = None, compression_level: int = 6) -> BackupResult
```

| Parameter | Type | Default |
|---|---|---|
| `site` | `str \| os.PathLike[str]` | required |
| `scope` | `BackupScope` | required |
| `target` | `StorageTarget \| str \| os.PathLike[str]` | required |
| `wp_config` | `str \| os.PathLike[str] \| None` | `None` |
| `credentials` | `CredentialSpec \| None` | `None` |
| `exclusion` | `ExclusionPolicy \| None` | `None` |
| `encrypt` | `bool` | `False` |
| `passphrase` | `Secret \| str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `dry_run` | `bool` | `False` |
| `on_warning` | `Callable[[RunWarning], None] \| None` | `None` |
| `db_tls` | `DbTls \| None` | `None` |
| `compression_level` | `int` | `6` |

Returns: `BackupResult`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`; `TypeError`, before anything runs, for arguments that do not fit the signature; `UsageError` naming `scope`, before anything runs, when `scope` is not a `BackupScope` (a string such as `"all"` included); `UsageError` naming `compression_level`, before anything runs, when it is not an `int` from 1 to 9 (a `bool` included).

### `verify`

Check `archive` without touching a Site; returns the passing result.

Keyless by default: a pass proves the checksums are consistent, not that
the Archive is authentic (ADR-0024). `authenticate=True` resolves the
passphrase and decrypts the payload, as `verify --authenticate` does; a
`passphrase` without `authenticate=True` is refused (`UsageError`) rather
than ignored, as `backup` refuses one without `encrypt=True`.
A failed verification raises `VerificationFailedError` carrying the
result, as the CLI exits `VERIFICATION_FAILED`. `on_warning` is accepted for
parity with `restore`; `verify` gives no notice today.

```python
verify(archive: StorageTarget | str | os.PathLike[str], *, authenticate: bool = False, passphrase: Secret | str | None = None, prompt: Callable[[str], str] | None = None, on_warning: Callable[[RunWarning], None] | None = None) -> VerificationResult
```

| Parameter | Type | Default |
|---|---|---|
| `archive` | `StorageTarget \| str \| os.PathLike[str]` | required |
| `authenticate` | `bool` | `False` |
| `passphrase` | `Secret \| str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `on_warning` | `Callable[[RunWarning], None] \| None` | `None` |

Returns: `VerificationResult`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`; `TypeError`, before anything runs, for arguments that do not fit the signature.

### `restore`

Restore `archive` into the Site at `site_root`; returns what was done.

`mode`, `overrides`, `snapshot` and `config_policy` have no
defaults (AD-5); FR-7's guard runs whichever root called. The passphrase
is resolved on every call: it decrypts an encrypted Archive and encrypts
the Safety Snapshot. `db_tls=None` requires no TLS, as WordPress connects
(owner decision 56); `relocation=None` is no relocation. A plain Archive
restored with a passphrase (`passphrase`, `L3IO_WP_BACKUP_PASSPHRASE` or
`prompt`) proceeds with WARNING `archive_not_encrypted`, a notice through
`on_warning`; `require_encrypted=True` refuses it with
`ArchiveNotEncryptedError` before any effect (owner decision 59).
`allow_unencrypted` is deprecated and has no effect (WARNING
`deprecated_option`). With no target `wp-config.php`, an encrypted Archive's
config gives the database without `archive_database` (owner decision 65,
noted in `RestoreOutcome.notes`); an unencrypted one still needs it.
Decryption accepts any passphrase length; one shorter than 4 characters
logs WARNING `credential_below_redaction_floor` (owner decision 69).
`relocation.new_db_password`, when not given, is read from
`L3IO_WP_BACKUP_NEW_DB_PASSWORD` if `relocation.new_db_user` is set. Notices go
to `on_warning` as `RunWarning` values when given, else to stderr. A real
restore whose database report does not say restored raises
`DatabaseNotRestoredError`, as the CLI exits `FAILED`. A process running as
root is refused with `RootRestoreRefusedError` before the Run opens: the
library never switches its host's user. The `wpbackup2` command started as
root switches to the Site Root's owner instead (owner decision 58). A Site
Root that does not look like WordPress (none of `wp-config.php`,
`wp-includes/`, `wp-content/`) and is not empty needs the files waiver in
`overrides`; one that does needs none (owner decision 57). Likewise a
database with tables none of which carries the site's table prefix needs the
database waiver; a WordPress or empty database needs none. A Mode that
restores the database loads the Dump straight into the live database, after
the Safety Snapshot, dropping the tables with the site's prefix first, and
reloads the snapshot if the load fails (`RollbackFailedError` when that fails
too; [ADR-0027](adr/0027-restore-into-an-environment-driven-database.md)).
`admin` is used only when the database does not exist yet or the WordPress
account lacks the rights (owner decision 55); without one such a restore
raises `AdminCredentialsRequiredError` before anything changes.
`compression_level` is the Safety Snapshot's payload gzip level, 1 to 9, as
`restore --compression-level`; 6 by default (owner decision 88).

**Forwarding user input.** `mode` and `overrides` are values, not the CLI's
flag-combination table (AD-25): `RestoreMode.ALL_CLEAN` clears the Site Root
and replaces the database as one value, and an `OverrideSet` can waive one
FR-7 refusal without the other. FR-7's guard, the Safety Snapshot, the Site
Lock and the consent checks still run. A host that builds `RestoreMode(...)`
or `Override(...)` from its own users' input takes on the choice the CLI makes
the operator spell out flag by flag, so it should check that input against
what those users may authorize.

```python
restore(site_root: str | os.PathLike[str], *, archive: StorageTarget | str | os.PathLike[str], mode: RestoreMode, overrides: OverrideSet, snapshot: SnapshotPolicy, config_policy: ConfigPolicy, snapshot_dir: str | os.PathLike[str] | None = None, admin: CredentialSpec | None = None, db_tls: DbTls | None = None, archive_database: str | None = None, reset_existing_db_user: bool = False, relocation: RelocationFlags | None = None, require_encrypted: bool = False, allow_unencrypted: bool = False, passphrase: Secret | str | None = None, prompt: Callable[[str], str] | None = None, resolve_run_id: str | None = None, dry_run: bool = False, on_warning: Callable[[RunWarning], None] | None = None, compression_level: int = 6) -> RestoreOutcome
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
| `db_tls` | `DbTls \| None` | `None` |
| `archive_database` | `str \| None` | `None` |
| `reset_existing_db_user` | `bool` | `False` |
| `relocation` | `RelocationFlags \| None` | `None` |
| `require_encrypted` | `bool` | `False` |
| `allow_unencrypted` | `bool` | `False` |
| `passphrase` | `Secret \| str \| None` | `None` |
| `prompt` | `Callable[[str], str] \| None` | `None` |
| `resolve_run_id` | `str \| None` | `None` |
| `dry_run` | `bool` | `False` |
| `on_warning` | `Callable[[RunWarning], None] \| None` | `None` |
| `compression_level` | `int` | `6` |

Returns: `RestoreOutcome`.

Raises: a `BackupError` subclass (below) for every failure the package recognises; `UnexpectedError`, with the original on `__cause__`, for any other `Exception`; `TypeError`, before anything runs, for arguments that do not fit the signature; `UsageError` naming the parameter, before anything runs, when `mode`, `overrides`, `snapshot` or `config_policy` is not of its type (an `OverrideSet` must waive `Override` members only), or `compression_level` is not an `int` from 1 to 9.

### `doctor`

FR-27: whether this host is ready, as `wpbackup2 doctor` reports it.

Returns the `Diagnosis`: `ready`, the tools found, the platform, the extras,
the passphrase backend by name, the archive target and, with `site`, that
Site's dump readiness. It raises nothing for what it reports, changes
nothing and takes no Site Lock.

```python
doctor(*, site: str | os.PathLike[str] | None = None, archive: StorageTarget | str | os.PathLike[str] | None = None) -> Diagnosis
```

| Parameter | Type | Default |
|---|---|---|
| `site` | `str \| os.PathLike[str] \| None` | `None` |
| `archive` | `StorageTarget \| str \| os.PathLike[str] \| None` | `None` |

Returns: `Diagnosis`.

Raises: `UnexpectedError`, with the original on `__cause__`, for any defect.

### `enumerate_defaults`

The default set, as `--list-default-excludes` and `api/` print it.

```python
enumerate_defaults() -> tuple[str, ...]
```

Returns: `tuple[str, ...]`.

## Values

### `prune`

Owner decisions 83 and 84: delete this Site's older Archives, keeping what the retention rules keep, as `wpbackup2 prune` does. `keep` keeps the newest N; `keep_daily`, `keep_weekly`, `keep_monthly` and `keep_yearly` each keep the newest Archive of each of the last N UTC days, ISO weeks, months or years that have one ([how retention works](docker.md#how-retention-works)). An Archive kept by any rule stays, and the newest always does. At least one rule must be given and each must be at least 1; otherwise `UsageError` before anything is read. Only Archives under `archive_dir` (`None`: the directory the CLI's backup writes to by default), flat or in subfolders (owner decision 86: up to 6 levels down, never through a symlinked folder), whose Manifest is this Site's count, by the matching `restore --archive latest` uses; a Safety Snapshot, another site's Archive, a file whose Manifest cannot be read, a symlink and a `.partial` file are never deleted. The subfolders the removals empty are removed too (`archive_dir_removed`; never `archive_dir` itself, nor a folder still holding anything). `dry_run=True` deletes nothing and returns what would go, the folders included. Takes the Site Lock. Each deletion is logged (`archive_pruned`, with `reasons`), each Archive kept at DEBUG (`archive_kept`, with the rules that keep it).

```python
prune(site: str | os.PathLike[str], *, keep: int | None = None, keep_daily: int | None = None, keep_weekly: int | None = None, keep_monthly: int | None = None, keep_yearly: int | None = None, archive_dir: str | os.PathLike[str] | None = None, wp_config: str | os.PathLike[str] | None = None, dry_run: bool = False) -> PruneResult
```

| Parameter | Type | Default |
|---|---|---|
| `site` | `str \| os.PathLike[str]` | required |
| `keep` | `int \| None` | `None` |
| `keep_daily` | `int \| None` | `None` |
| `keep_weekly` | `int \| None` | `None` |
| `keep_monthly` | `int \| None` | `None` |
| `keep_yearly` | `int \| None` | `None` |
| `archive_dir` | `str \| os.PathLike[str] \| None` | `None` |
| `wp_config` | `str \| os.PathLike[str] \| None` | `None` |
| `dry_run` | `bool` | `False` |

Returns: `PruneResult`.

Raises: `UsageError` (no rule, a rule below 1, a missing Archive directory), `SiteLockedError`, `PruneFailedError` (a chosen Archive could not be removed; the others were), `RootPruneRefusedError` (running as root; a `RootBackupRefusedError`), and `UnexpectedError`, with the original on `__cause__`, for any defect.

### `Diagnosis`

FR-27's diagnostic: what is present, never an environment value. Fields:
`python`, `platform`, `platform_supported`, `tools` (one `ToolStatus` per
known binary), `database_clients`, `passphrase_backend` (`auto`, `binder`,
`cli` or `invalid`), `extras`, `storage_target` and `ready`. `to_dict()` is
the JSON `wpbackup2 doctor --json` prints. `ready` is false for an
unsupported platform, a present but unusable tool, an unwritable or occupied
target, or a Site whose database is not ready.

### `RunWarning`

Defined in the CLI layer; import it from l3io.wp.backup.api.

An operator notice, as a value: what `restore` writes to stderr when no
`on_warning` is given. Fields: `code` (the WARNING event name:
`archive_unauthenticated`, `safety_snapshot_unencrypted`,
`safety_snapshot_encrypted`), `message` (the line the CLI prints) and
`fields` (paths and the integrity level as strings). It never carries a
secret.

```python
RunWarning(code: str, message: str, fields: Mapping[str, str] = {})
```

### `Secret`

A password or passphrase that never shows itself (owner decision
2026-11-07). `repr` and `str` are `Secret('***')`; `reveal()` returns the
text. It is not equal to, nor hashable as, its plain string; two `Secret`s
compare in constant time; pickling one raises `TypeError`. Every password or
passphrase parameter and field (`passphrase=`, `ExplicitCredentialSpec.password`,
`RelocationFlags.new_db_password`) accepts a `Secret` or a `str`, and a `str`
is wrapped at once.

```python
Secret(value: str | Secret)
```

`value` is the text to hold (a `Secret` is copied).

### `DbTls`

The TLS every database connection of a Run uses (owner decision 56). `mode`
is `required`, `verify-ca` or `verify-identity`, or `None` for no
requirement, as WordPress connects; `ca_file` verifies the server (with a
verifying mode), `None` meaning the system store.

```python
DbTls(mode: str | None = None, ca_file: Path | None = None)
```

| Field | Type | Default |
|---|---|---|
| `mode` | `str \| None` | `None` |
| `ca_file` | `Path \| None` | `None` |

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

### `PruneResult`

What a prune did, or in a Dry Run would do (owner decisions 83, 84): `directory`, `policy` (the `RetentionPolicy` applied), `kept` (one `KeptArchive` per Archive of this Site's left, newest first, with the rules that keep it), `pruned` (one `PrunedArchive` per Archive removed, newest first, with why), `skipped` (each `*.tar.gz` entry that was no candidate for what it is, with the reason), `considered` (Archives read), `matched` (this Site's), `performed` (`False` in a Dry Run) and `removed_directories` (the subfolders of the Archive directory the removals emptied and the prune removed, deepest first; owner decision 86; in a Dry Run, those it would remove).

```python
PruneResult(directory: PurePosixPath, policy: RetentionPolicy, kept: tuple[KeptArchive, ...], pruned: tuple[PrunedArchive, ...], skipped: tuple[tuple[PurePosixPath, str], ...], considered: int, matched: int, performed: bool, removed_directories: tuple[PurePosixPath, ...] = ())
```

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `PrunedArchive`

One Archive a prune removed, or would remove: `path`, `bytes`, `created_at` (its Manifest's) and `reasons`, one per rule given, saying why that rule does not keep it (`"daily: not the newest of its day"`, `"weekly: older than the newest 4 weeks that have an archive"`, `"newest: not among the newest 14"`).

```python
PrunedArchive(path: PurePosixPath, bytes: int, created_at: str, reasons: tuple[str, ...])
```

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `KeptArchive`

One of this Site's Archives a prune left: `path`, `created_at` (its Manifest's) and `reasons`, the rules that keep it: `"newest"` (`keep`), `"daily"`, `"weekly"`, `"monthly"`, `"yearly"`.

```python
KeptArchive(path: PurePosixPath, created_at: str, reasons: tuple[str, ...])
```

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `RetentionPolicy`

The retention rules a prune applies (owner decision 84): `keep`, `keep_daily`, `keep_weekly`, `keep_monthly`, `keep_yearly`, each `None` (not used) or at least 1, at least one given; otherwise `UsageError` naming the setting(s). `rules()` returns the rules given with their counts, `settings()` them by setting name (`{"keep_daily": 7}`), `describe()` them as flags (`--keep-daily 7 --keep-weekly 4`). `prune` builds one from its arguments; `PruneResult.policy` is the one applied.

```python
RetentionPolicy(keep: int | None = None, keep_daily: int | None = None, keep_weekly: int | None = None, keep_monthly: int | None = None, keep_yearly: int | None = None)
```

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
ExplicitCredentialSpec(username: str, password: Secret | str)
```

`password` is held as a `Secret` (a `str` is wrapped at once). A password
shorter than 4 characters is accepted with WARNING
`credential_below_redaction_floor`: too short to be masked in logs (owner
decision 61).

| Field | Type | Default |
|---|---|---|
| `username` | `str` | required |
| `password` | `Secret \| str` | required |

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
RelocationFlags(*, new_site_url: str | None, new_home_url: str | None, new_db_host: str | None, new_db_name: str | None, new_db_user: str | None, new_db_password: Secret | str | None, wp_config: Path | None)
```

`new_db_password` is held as a `Secret`. The CLI fills it from
`--new-db-password`, `--new-db-password-file` or
`L3IO_WP_BACKUP_NEW_DB_PASSWORD`, in that order; `restore` reads that variable
when the field is `None` and `new_db_user` is set. Each source is registered for
redaction and held to the 4-character floor.

| Field | Type | Default |
|---|---|---|
| `new_site_url` | `str \| None` | required |
| `new_home_url` | `str \| None` | required |
| `new_db_host` | `str \| None` | required |
| `new_db_name` | `str \| None` | required |
| `new_db_user` | `str \| None` | required |
| `new_db_password` | `Secret \| str \| None` | required |
| `wp_config` | `Path \| None` | required |

`RelocationFlags.none()` gives no relocation flag; `restore(relocation=None)` means the same.

Defined in the CLI layer; import it from l3io.wp.backup.api.

### `RestoreMode`

FR-6's Restore Modes. Resolved once from the flags, carried as a value.

Members: `FILES` (`'files'`), `FILES_CLEAN` (`'files-clean'`), `DATABASE` (`'database'`), `DATABASE_CLEAN` (`'database-clean'`), `ALL` (`'all'`), `ALL_CLEAN` (`'all-clean'`).

### `RestoreOutcome`

What one restore did (or, in a Dry Run, planned); built, never printed.

```python
RestoreOutcome(*, run_id: str, mode: RestoreMode, phases: tuple[RestorePhase, ...], dry_run: bool, actions: tuple[PlannedRestoreAction, ...], integrity: Integrity | None, snapshot_path: PurePosixPath | None, snapshot_encrypted: bool, database: DatabaseRestoreReport | None, search_replace: SearchReplaceReport | None, resolved_run_id: str | None, skipped_links: tuple[SkippedLink, ...] | None, wp_config_kept: bool, wp_config_absent: bool = False, notes: tuple[str, ...] = ())
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
| `skipped_links` | `tuple[SkippedLink, ...] \| None` | required |
| `wp_config_kept` | `bool` | required |
| `wp_config_absent` | `bool` | `False` |
| `notes` | `tuple[str, ...]` | `()` |

`wp_config_absent` is true when the target had no `wp-config.php` and the Run
wrote none: a database-only restore onto such a folder (owner decision 74).

`notes` are what the Run chose without asking, each also logged: the
Archive `--archive latest` named (owner decision 64) and an encrypted
Archive's database used as its config gives it (owner decision 65). A Dry
Run prints them before its plan.

`skipped_links` holds the symlinks extraction skipped. In a Dry Run it holds
the ones the real Run would skip, and is `None` when the Archive (v0) records
no link targets, so they are known only at extraction (owner decision 41).

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

A call whose arguments do not fit the entry point's signature (a missing
required keyword such as `backup`'s `target`, an unknown keyword, too many
positional arguments) raises Python's own `TypeError`, before anything runs and
before a Run opens. It is not a `BackupError`, is not logged and is not wrapped
in `UnexpectedError`: AD-14 governs the failures a call reaches, and this one
never reaches the package.

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

Owner decision 55: a restore needs an administrator only when the database
does not exist yet or the WordPress account lacks the rights. `missing` then
names what is missing, as finding codes (`database_absent`,
`missing_privilege`, `authentication_failed`, `database_not_visible`),
`database` the database and `account` the WordPress account's credential
source; never a secret.

```python
AdminCredentialsRequiredError(source: str, *, flags: tuple[str, ...], missing: tuple[str, ...] = (), database: str | None = None, account: str | None = None)
```

Base: `CredentialsError`. Exit code: `CREDENTIALS` (4).

Fields: `source`, `flags`, `missing`, `database`, `account`, each holding the argument of the same name.

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

Also raised by `restore` for a plain Archive when `require_encrypted=True`
(`required`). A plain Archive restored with a passphrase is not refused: it
proceeds with WARNING `archive_not_encrypted` (owner decision 59).

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `archive`, `required`, each holding the argument of the same name.

### `ArchiveNotFoundError`

The Archive does not exist or cannot be opened.

```python
ArchiveNotFoundError(path: PurePath)
```

Base: `BackupError`. Exit code: `USAGE` (2): the path the caller named is not there (owner decision 2026-11-07, BL-E002-074).

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

### `ChildOutputLimitError`

A child printed more than `limit` bytes on one stream and was killed.

```python
ChildOutputLimitError(limit: int)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `limit`, holding the argument of the same name.

### `CorruptJournalError`

`current.json` exists and cannot be read back (NFR-1: never absent).

`--resolve-interrupted-run` cannot act on it either, since no Run ID or
phase can be read; the message names the manual way out (R10).

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

A database check found blockers; `findings` are codes only. `database` names the database the check was about and `account` the account it answered for (a user name, or `administrator`); either is `None` where not known. Neither is ever a secret.

```python
DatabaseRefusedError(findings: tuple[str, ...], *, database: str | None = None, account: str | None = None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `findings`, `database`, `account`, each holding the argument of the same name.

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

### `EnvironmentDrivenTargetError`

A restore target's `wp-config.php` reads one of `DB_HOST`, `DB_USER`, `DB_PASSWORD` or `DB_NAME` from the environment (`getenv_docker()` or `getenv()`, as the official `wordpress` image writes them), and a relocation (`--new-db-*`) would rewrite it as a fixed value. Refused before any change, a Dry Run included ([ADR-0027](adr/0027-restore-into-an-environment-driven-database.md), Accepted). `keys` are the constants, `variables` the environment variables they read (names only), `path` the file and `operation` what was refused. A restore without relocation resolves the lookups and loads the database they name, writing no `DB_*` (owner decisions 47 and 49); with `config_policy=ConfigPolicy.OVERWRITE` a files restore carries them into the Archive's file as written (owner decision 48).

```python
EnvironmentDrivenTargetError(*, keys: tuple[str, ...], variables: tuple[str, ...], path: PurePath, operation: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `keys`, `variables`, `path`, `operation`, each holding the argument of the same name.

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

### `ExtractionRefusedAfterCleanError`

Extraction refused a member after `CLEAN` had cleared the Site Root, so the Site was changed; the message says so and names the Safety Snapshot (BL-E002-069).

```python
ExtractionRefusedAfterCleanError(member: str | None, reason: str, *, snapshot: str | None)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `member`, `reason`, `snapshot`, each holding the argument of the same name.

### `ExtractionRefusedError`

Extraction refused the Archive: an unsafe member, or no `data` filter.

```python
ExtractionRefusedError(member: str | None, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `member`, `reason`, each holding the argument of the same name.

### `ExtractionWriteError`

Extracting the Archive could not write into its destination: the Site Root, or the directory a laid-down `wp-config.php` goes into. The message names which (`could not write into the Site Root '/var/www/html': Permission denied`). A subclass of `ArchiveWriteError`, so its exit code is unchanged. A restore's target survey now refuses a target its user cannot write before any effect (`TargetNotWritableError`), so this is left for what changes after that check.

```python
ExtractionWriteError(path: PurePath, reason: str, *, destination: str)
```

Base: `ArchiveWriteError`. Exit code: `FAILED` (1).

Fields: `path`, `reason`, `destination`, each holding the argument of the same name.

### `FlagCombinationRejectedError`

AD-25 — restore flags that express no Restore Mode.

```python
FlagCombinationRejectedError(*, reason: str, use_instead: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `reason`, `use_instead`, each holding the argument of the same name.

### `IncompleteInstallError`

A required dependency cannot be imported: the installation is broken. The
family (`l3io-wordpress-database`, `l3io-wordpress-config`, the
`l3io-age-passphrase` binder) is a required dependency since 1.0.0 (owner
decision 76), so a pip install always brings it; one made with `--no-deps`, or
an environment pruned afterwards, can still lack it. No extra would fix it:
reinstall `l3io-wordpress-backup`. Raised at the first use, before any write.

```python
IncompleteInstallError(distribution: str)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `distribution`, the missing distribution's name.

### `IncompleteRelocationCredentialsError`

FR-11 — half of the relocated account was given.

```python
IncompleteRelocationCredentialsError(*, given: str, missing: str, arguments: tuple[str, ...] | None = None)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `given`, `missing`, each holding the argument of the same name; `arguments`, the two parameters (`relocation.new_db_user`, `relocation.new_db_password`), `(given, missing)` when not given.

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
InvalidRelocationValueError(flag: str, rule: str, *, argument: str | None = None)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `flag`, `rule`, each holding the argument of the same name; `argument`, the parameter at fault (`relocation.new_site_url`), `flag` when not given; `arguments` is `(argument,)`.

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

### `LoadCheckFailedError`

The loaded database is not a WordPress database (the post-load check).

The load went into the live database (owner decision 49), so the automatic
rollback reloads the Safety Snapshot before this is raised (decision 50);
when that fails, `RollbackFailedError` is raised instead. Renamed from
`StagingCheckFailedError`.

```python
LoadCheckFailedError(database: str, *, is_wordpress: bool, schema_version: str | None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `database`, `is_wordpress`, `schema_version`, each holding the argument of the same name.

### `LoadFailedError`

Loading a Dump started and failed, or the Dump was unreadable.

```python
LoadFailedError(reason: str, *, database: str, exit_status: int | None = None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `database`, `exit_status`, each holding the argument of the same name.

### `LoadReadinessError`

The load-readiness check, run as the WordPress account, could not run.

```python
LoadReadinessError(reason: str, *, database: str, source: str)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `reason`, `database`, `source`, each holding the argument of the same name. `check_load` answers as the WordPress account the load runs as, never the administrator (owner decision 2026-11-07); `source` names that account's credential source, never a value.

### `MalformedManifestError`

A Manifest's JSON is not a valid v1 Manifest.

```python
MalformedManifestError(reason: str, *, field: str | None)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `reason`, `field`, each holding the argument of the same name.

### `ManifestTooLargeError`

The Manifest this backup would write exceeds the 256 MiB cap; refused after
the Site Root is walked and before anything is written (BL-E004-053).

```python
ManifestTooLargeError(*, members: int, size: int, limit: int)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `members`, `size`, `limit`, each holding the argument of the same name.

### `MemberReadRefusedError`

A single-member read was refused: not a regular file, not indexed, too big.

```python
MemberReadRefusedError(path: str, reason: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `reason`, each holding the argument of the same name.

### `MissingExtraError`

An optional feature was used without its extra installed (AD-8): today the
`aws` extra, when an AWS Secrets Manager secret is resolved. A required
dependency missing from the installation is `IncompleteInstallError`, never this.

```python
MissingExtraError(extra: str)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `extra`, each holding the argument of the same name.

### `NoConfigForCutoverError`

A relocation's cutover, and no `wp-config.php` will exist to write it.

`path` is where the cutover would write; `searched` is every place a target
`wp-config.php` was looked for (`path` alone when not given).

```python
NoConfigForCutoverError(path: str, *, searched: tuple[str, ...] = ())
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `searched`, each holding the argument of the same name.

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

### `PassphraseMismatchError`

The prompted encryption passphrase was not confirmed: the two entries differed.

```python
PassphraseMismatchError(*, prompts: int = 2)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `prompts`, holding the argument of the same name.

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

Base: `RefusedError`. Exit code: `CREDENTIALS` (4): a missing key is a credentials failure (owner decision 2026-11-07, BL-E002-069).

Fields: `operation`, each holding the argument of the same name.

### `PassphraseUnavailableError`

AD-26 — passphrase encryption was requested and no usable one resolved.

```python
PassphraseUnavailableError(variable: str, *, reason: str, argument: str | None = None)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `variable`, `reason`, `argument`, each holding the argument of the same name.
`argument` is the passphrase argument the hint names: `passphrase` from the
library, `None` from the CLI, which takes no passphrase value.

### `PassphraseTooShortError`

An encryption passphrase below 12 characters (ADR-0024 amendment); decryption
has no floor.

```python
PassphraseTooShortError(*, minimum: int)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `minimum`, holding the argument of the same name.

### `PayloadDamagedError`

The encrypted payload is damaged, truncated or not `age`.

```python
PayloadDamagedError(field: str | None = None)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `field`, each holding the argument of the same name.

### `PreflightFailedError`

A tool the Run needs is absent or unusable.

`minimum` is the version the tool must reach, set when the failure is a
too-old or unknown version, so the message states the floor.

```python
PreflightFailedError(tool: str, needed_for: str, found: str, *, install_hint: str | None = None, minimum: str | None = None)
```

Base: `BackupError`. Exit code: `NOT_READY` (7).

Fields: `tool`, `needed_for`, `found`, `install_hint`, `minimum`, each holding the argument of the same name.

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
RelocationNeedsDatabaseRestoreError(*, flags: tuple[str, ...], mode: str, arguments: tuple[str, ...] | None = None)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `flags`, `mode`, each holding the argument of the same name; `arguments`, the parameters (`relocation.<field>`), `flags` when not given.

### `RootRestoreRefusedError`

A restore as root that cannot run as the Site Root's owner (owner decisions 58, 68): the `wpbackup2` command refuses a Site Root owned by root (there is no owner to switch to), and the library refuses any restore running as root (it never switches its host's user). Raised before the Run opens.

```python
RootRestoreRefusedError(*, site_root: str, reason: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `site_root`, `reason`, each holding the argument of the same name; `arguments` is `("site_root",)`.

### `PrivilegeDropError`

The `wpbackup2` command, started as root, switched to the Site Root's owner (for a restore or a backup) and the switch did not hold: an id read back unchanged, a supplementary group remained, or root could be regained (owner decisions 58, 67). Raised before the Run opens; nothing was changed.

```python
PrivilegeDropError(*, uid: int, gid: int, step: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `uid`, `gid`, `step`, each holding the argument of the same name.

### `RollbackFailedError`

Decision 50: the load failed, and putting the database back failed too. Also
decision 72: the content URL rewrite (`SEARCH_REPLACE`) after a relocation
failed, and putting the database or the original `wp-config.php` back failed.

`cause` is the load's (or the rewrite's) failure and `rollback` the rollback's (`None` when
nothing could roll back: no Safety Snapshot); `database` the live database,
now partly loaded; `snapshot` the Safety Snapshot's path (`None` without one)
and `recovery` the commands that restore it. The Run stays recorded as
interrupted. Names, paths, error classes and this package's own messages
only.

```python
RollbackFailedError(*, cause: BaseException, rollback: BaseException | None, database: str, snapshot: str | None, recovery: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `cause`, `rollback`, `database`, `snapshot`, `recovery`, each holding the argument of the same name.

### `PruneFailedError`

A prune could not remove some of the Archives it chose (one changed after it was read, or the operating system refused); the others were removed, and each failure was logged (`archive_prune_failed`).

```python
PruneFailedError(*, failures: tuple[tuple[str, str], ...])
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `failures` (each `(path, reason)`), holding the argument of the same name.

### `RootBackupRefusedError`

A backup as root that cannot run as the Site Root's owner (owner decisions 67, 68): the `wpbackup2` command refuses a Site Root owned by root (there is no owner to switch to), and the library refuses any backup running as root (it never switches its host's user; drop privileges first, or use the command). Raised before the Run opens.

```python
RootBackupRefusedError(*, site_root: str, reason: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `site_root`, `reason`, each holding the argument of the same name; `arguments` is `("site_root",)`.

### `RootPruneRefusedError`

A prune as root that cannot run as the Site Root's owner (owner decisions 67, 68, 83), refused in a prune's words ("refused to prune as root in ..."): the `wpbackup2` command refuses a Site Root owned by root, and the library refuses any prune running as root (`api.prune`; drop privileges first, or use the command). Raised before the Run opens. A `RootBackupRefusedError`, so code that catches the backup's refusal, all a prune raised in 1.1, still catches it.

```python
RootPruneRefusedError(*, site_root: str, reason: str)
```

Base: `RootBackupRefusedError`. Exit code: `USAGE` (2).

Fields: `site_root`, `reason`, each holding the argument of the same name; `arguments` is `("site_root",)`.

### `ArchiveDirectoryNotWritableError`

The `wpbackup2` command, started as root, switched to the Site Root's owner for a backup or a prune, and that owner cannot write the Archive directory (the target's directory, `--archive-dir`, or the default one); a directory not made yet is checked where it would be made (owner decision 67). Raised after the switch and before the Run opens, a Dry Run too; nothing was changed.

```python
ArchiveDirectoryNotWritableError(
    *, directory: str, uid: int, user: str | None, operation: str = "backup"
)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `directory`, `uid`, `user`, `operation`, each holding the argument of the same name (`user` is `None` when the uid has no password entry; `operation` is `"backup"` or `"prune"`, the Run that was refused).

### `RelocationTargetNotEmptyError`

A relocation's load database (`--new-db-name`, or the Site's name on the `--new-db-host` server) already holds tables. It must be missing (the Run creates it, and drops it again on failure) or empty: the Safety Snapshot covers only the live database, so nothing is overwritten without one. Refused before any change; `--force` does not waive it (owner decision 73).

```python
RelocationTargetNotEmptyError(*, database: str, tables: int)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `database` (`host:port/name`) and `tables` (how many it holds), each holding the argument of the same name.

### `InstallationNotReadableError`

The `wpbackup2` command, started as root, switched to the Site Root's owner (for a restore or a backup), and that owner cannot read a directory of the installation it runs from: the standard library, `site-packages` or another `sys.path` entry after the first, as an interpreter or virtual environment under root's home directory leaves them (`uv tool install` or `pipx install` run as root). Python imports a module on first use, so the Run would otherwise fail part way through, reported as an incomplete installation. Raised right after the switch and before the Run opens, a Dry Run too; nothing was changed (owner decisions 58, 67).

```python
InstallationNotReadableError(*, path: str, uid: int, user: str | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `uid`, `user`, each holding the argument of the same name (`path` is the first directory the owner cannot read; `user` is `None` when the uid has no password entry).

### `NoArchiveForSiteError`

`--archive latest` found no Archive of this Site in the Archive directory (owner decision 64): none whose Manifest records this Site Root, database host, port and name and table prefix. Raised by the `wpbackup2` command before the Run opens.

```python
NoArchiveForSiteError(*, directory: PurePath, considered: int, site: str)
```

Base: `UsageError`. Exit code: `USAGE` (2).

Fields: `directory`, `considered`, `site`, each holding the argument of the same name; `arguments` is `("archive",)`.

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

### `SiteRootNotFoundError`

The Site Root the caller named (`--wp-dir`, `site`, `site_root`) does not exist (BL-E002-074).

```python
SiteRootNotFoundError(path: str)
```

Base: `InvalidSiteDescriptorError`. Exit code: `USAGE` (2).

Fields: `path`, holding the argument of the same name.

### `SnapshotFailedError`

FR-8: the Safety Snapshot was not written and verified; nothing destroyed.

```python
SnapshotFailedError(path: str | None, cause: BaseException | str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path`, `cause`, each holding the argument of the same name.

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

### `TargetNotWritableError`

A restore would write where the user it runs as cannot: the Site Root, an existing directory the Archive's files go into, an existing file they replace, a directory `--cleanfirst` clears, or where a laid-down `wp-config.php` is created. Found by the target survey before any effect, a Dry Run too; `--force` does not waive it. The usual cause is an Archive restored by a user other than the Site Root's owner: run the restore as root (in the Docker image, `PUID=0`), and the command switches to the Site Root's owner (owner decision 58).

```python
TargetNotWritableError(*, paths: tuple[str, ...], uid: int | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `paths` (every path found, sorted; the message names the first three) and `uid` (the effective uid checked, `None` when not known), each holding the argument of the same name.

### `PathNotAccessibleError`

A path the Run needs is not readable or writable by the user it runs as (owner decision 87). The `wpbackup2` command checks, before the Run opens and a Dry Run too, after any switch to the Site Root's owner: the per-user state directory (write; `backup`, `restore`, `prune`), `TMPDIR` when it is set (write; `backup`, `restore`), the Archive directory (write; `backup`, `prune`), a backup's Site Root (read), a restore's `--snapshot-dir` (write), and the Archive a `restore` or `verify` opens (read; it was `ArchiveNotFoundError`, exit 2, when permission was denied). A path not made yet is checked at its nearest existing folder, where it would be made. wpbackup2 never changes ownership: the message names the path, the effective `uid:gid`, the owner, group and mode found, and the fix (`chown -R uid:gid` on the host folder or volume, or run as the owner: `PUID`/`PGID` or `--user` in the Docker image). Nothing was changed.

```python
PathNotAccessibleError(*, path: str, role: str, access: Literal["read", "write"], uid: int, gid: int, checked: str, owner_uid: int | None, owner_gid: int | None, mode: int | None)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `path` (the path needed), `role` (what it is, e.g. `state directory`), `access`, `uid` and `gid` (the effective ids), `checked` (the path examined: the path itself, its nearest existing folder, or the first folder above it the user cannot enter), `owner_uid`, `owner_gid` and `mode` (`checked`'s; `None` when it could not be examined), each holding the argument of the same name.

### `UnboundAdapterError`

A port whose real implementation has not landed was called. A defect.

```python
UnboundAdapterError(port: str, *, supplier: str)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `port`, `supplier`, each holding the argument of the same name.

### `UndefinedConfigFunctionError`

`--overwrite-wp-config` (`config_policy=ConfigPolicy.OVERWRITE`) carries the target's `DB_*` definitions into the Archive's `wp-config.php` as written, and the target reads one of them through `function` (`getenv_docker`, the official `wordpress` image's helper, which PHP itself does not define), but the Archive's file does not define that function before the carried use: PHP would stop with "Call to undefined function". Refused before any change, a Dry Run included (owner decision 48). `keys` are the constants, `path` the target's `wp-config.php`. Whether the Archive's file declares the function is `l3io-wordpress-config`'s lexical `declares_function` (0.1.7); [docs/restore.md](restore.md) lists the limits it documents. Omit `--overwrite-wp-config` to keep the target's file.

```python
UndefinedConfigFunctionError(*, function: str, keys: tuple[str, ...], path: PurePath)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `function`, `keys`, `path`, each holding the argument of the same name.

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

### `UnsupportedConfigValueError`

A `wp-config.php` setting the Run reads (`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` or `$table_prefix`) is a PHP expression other than a literal, `getenv_docker('NAME', <literal>)` or `getenv('NAME')`. This package does not evaluate PHP, so it is refused before any connection, never guessed at (owner decision 47). `key` names the setting and `path` the file; the expression's text is never carried, since it may compute a credential.

```python
UnsupportedConfigValueError(key: str, *, path: PurePath)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `key`, `path`, each holding the argument of the same name.

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

`arguments` names the arguments at fault in one vocabulary (BL-E006-015): on
every path a library call reaches, the library parameter's name
(`config_policy`, `archive_database`, `admin_tls`), dotted for a field of one
(`admin_tls.ca_file`, `relocation.new_site_url`); an environment variable by
its name. Only the command line's own argument checks, which no library call
reaches, name its flags. The message may also name the flag.

### `VerificationFailedError`

An Archive failed verification; `result` says why.

`archive` names the Archive the operator gave, when the raiser knows it,
so a job verifying several can tell which failed (BL-E002-055).

```python
VerificationFailedError(result: VerificationResult, archive: str | None = None)
```

Base: `BackupError`. Exit code: `VERIFICATION_FAILED` (8).

Fields: `result`, `archive`, each holding the argument of the same name.

### `WorkFactorTooHighError`

The encrypted payload's scrypt work factor is above what this host decrypts,
so it was refused before any work was done (1.1.2).

The `age` header names the work factor (log2), and the decrypting backend has
a fixed ceiling: 22 for the `l3io-age-passphrase` binder (0.1.3) and for stock
`age`. `wpbackup2` 1.1.2 and later write 18, so an Archive they wrote does
not exceed it. Nothing says the payload is damaged, so this is not
`PayloadDamagedError` and not a verification failure: decrypt the payload with
the tool that encrypted it. Logged first as ERROR `member_decryption_failed`
with `reason` `work_factor_too_high`. Database's `WorkFactorTooHighError`
(`l3io-wordpress-database` 0.3.6) is the cause.

```python
WorkFactorTooHighError(*, scheme: str)
```

Base: `RefusedError`. Exit code: `REFUSED` (3).

Fields: `scheme`, holding the argument of the same name (`age`).

### `WorkspaceNotRemovedError`

The Run's workspace survived its removal, and may hold a plaintext Dump.

Raised only when the Run had otherwise succeeded (BL-E002-041), so it
does not exit `OK` with a copy of the database left behind; a failing
Run keeps its own error and the WARNING. `path` is the workspace; the
message names only its prefix, since the path is logged at `DEBUG` only.

```python
WorkspaceNotRemovedError(path: Path)
```

Base: `BackupError`. Exit code: `FAILED` (1).

Fields: `path`, holding the argument of the same name.

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
