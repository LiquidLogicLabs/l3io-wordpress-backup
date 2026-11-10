# Changelog

Behaviour changes an operator of `wpbackup2` v0.2.21 must know about. The
release notes proper are built from commit subjects
(`.github/changelog-config.json`); this file carries the named entries the
owner asked for, which a commit subject cannot hold.

## 1.0.0 — 2026-11-10

### Changed: the family resolves from PyPI (2026-11-10)

- `pip install 'l3io-wordpress-backup[database]'` installs from PyPI alone, with
  no extra index: `l3io-wordpress-database` (>=0.3.5), `l3io-wordpress-config`
  (>=0.1.7) and the `l3io-age-passphrase` binder (>=0.1.1) are published there.
  Development and CI took them from TestPyPI until now; that index is no longer
  used anywhere in the dependency path.

### Changed: relocation safety, owner decisions 72-75 (2026-11-09)

- **A failed URL rewrite rolls the relocation back** (decision 72). When
  `SEARCH_REPLACE` (`wp search-replace`, after `--new-site-url` or
  `--new-site-home-url`) fails, the Run puts the database back as a failed
  load does (the Safety Snapshot reloaded into the live database; a
  `--new-db-name` database dropped again when the Run made it, emptied
  otherwise) and copies the original `wp-config.php` back from the copy it now
  saves before `REWRITE_CONFIG` (beside the Safety Snapshots), then exits with
  the rewrite's error and WARNING `relocation_rolled_back`. A `--mode db` Run
  is then not left interrupted. A rollback that fails is `RollbackFailedError`
  (exit 1), as for a failed load. Before, the Run stopped with the database
  loaded, the URLs half rewritten and `wp-config.php` pointing at the new
  site, recorded as interrupted.
- **`--new-db-name` must name a missing or empty database** (decision 73). A
  relocation's load database (also the Site's name on a `--new-db-host`
  server) that holds tables is refused before anything changes, naming it
  (exit 3, `RelocationTargetNotEmptyError`, new); `--force` no longer waives
  it, since the Safety Snapshot covers only the live database. A missing one
  is created and, if the Run fails, dropped again, now also when the failure
  comes before the load starts.
- **A database-only restore onto a folder without `wp-config.php` says it wrote
  none** (decision 74): `RestoreOutcome.wp_config_absent` (new, default
  `False`), the JSON report's `wp_config_absent`, the line "wp-config.php:
  none at the target, and none written", and INFO `wp_config_not_written`.
- **Dead database ports removed** (decision 75): `revoke_grants`,
  `check_revoke` and `check_drop_database`, which no restore has called since
  the direct load, are gone from the database port and its adapters (and the
  never-emitted `grants_revoked` log event from `docs/logging.md`). No
  behaviour changes.

### Changed: a root Run refuses an installation its site owner cannot read (2026-11-09)

- **Checked right after the switch to the Site Root's owner** (owner
  decisions 58, 67). Python imports a module on first use, so a `wpbackup2`
  installed under root's home directory (`uv tool install` or `pipx install`
  run as root: the interpreter or the virtual environment in `/root`, mode
  `0700`) could not load the rest of itself once it ran as the site's user,
  and the Run failed part way through as "this feature needs the 'database'
  extra" (exit 7). Each directory of `sys.path` after the first is now
  checked after the switch; one the owner cannot read refuses the Run before
  it opens, a Dry Run too (exit 3, `InstallationNotReadableError`, naming the
  directory). Install `wpbackup2` where the site's user can read it, or run
  it as that user. `InstallationNotReadableError` is new.
- **`--help` shows an option with two spellings the same on every Python**:
  `--admin-user, --admin-username USER`, as CPython 3.13 and later print it;
  3.11 and 3.12 repeated the metavar after each spelling.

### Changed: a database restore loads straight into the live database (owner decisions 49-55, 57, 63, 71; ADR-0027, 2026-11-09)

- **No staging database, no `DB_NAME` change** (decisions 49, 55). A restore
  that loads the database loads the Dump straight into the live database
  `wp-config.php` names, as the site's WordPress account, after the Safety
  Snapshot. v0.2.21 and the earlier pre-releases loaded into
  `<name>_l3s_<run>` and switched `wp-config.php`'s `DB_NAME` to it, so the
  site ran on a new database name after every restore; it now keeps its
  name, and `--archive latest` finds the site's Archives after a restore.
  The site is down for the length of the load.
- **The database matches the Archive** (decisions 52, 53): every table and
  view whose name starts with the site's table prefix is dropped before the
  load, so a table added after the backup (a plugin's) is gone. Tables
  without the prefix are never touched. The dropped tables are logged
  (`database_tables_dropped`), printed in the report and listed in the Dry
  Run, whose `LOAD_DUMP` line also names the host and database overwritten.
- **Automatic rollback** (decision 50): a failure after the load began
  reloads the Safety Snapshot into the live database (WARNING
  `database_rolled_back`) and exits with the load's error. If the rollback
  fails, or there is no snapshot (`--no-safety-snapshot`), the Run exits 1
  with the new `RollbackFailedError`, naming both failures, the snapshot and
  the recovery commands, and stays recorded as interrupted.
- **An administrator only when needed** (decision 55): only when the
  database does not exist yet or the WordPress account cannot log in or lacks
  a right. Without one, such a restore is refused before anything changes
  (exit 4, `AdminCredentialsRequiredError`, now naming the database, the
  account and what is missing). Previously every database restore needed one.
  Started as root, a restore resolves a configured administrator (an AWS
  profile in root's home included) before it switches to the Site Root's
  owner (decision 71).
- **`--force` for a database** (decision 57): only a database with tables
  none of which carries the site's prefix needs it; a WordPress database or
  an empty one does not. Previously any table needed it.
- **Environment-driven `DB_*` restore directly** (decision 47): a target
  whose `wp-config.php` reads `DB_*` with `getenv_docker()`/`getenv()` (the
  official `wordpress` image) is no longer refused with
  `EnvironmentDrivenTargetError`; the database the environment names is
  loaded and `wp-config.php` is left as it is. `EnvironmentDrivenTargetError`
  now refuses only a relocation flag (`--new-db-*`) that would rewrite such
  a constant.
- **`--drop-database` is deprecated and has no effect** (decision 63): there
  is no previous database left to remove. It is accepted and logs WARNING
  `deprecated_option`; `--mode db --drop-database` still names the Mode
  `database-clean`, which now restores exactly as `database` does.
  `--mode all --cleanfirst` alone now selects `all-clean` (it was refused
  without `--drop-database`), and `--mode all --drop-database` or `--mode
  files --drop-database` now select what the same flags without it select
  (they were refused).
- **A relocation** (`--new-db-name`, `--new-db-host`) loads its database the
  same way (prefixed tables dropped first) and still cuts `wp-config.php`
  over to it; a database-only restore onto a target with no `wp-config.php`
  no longer needs one (`NoConfigForCutoverError` is a relocation's only).
- **Library:** `StagingCheckFailedError` is renamed `LoadCheckFailedError`;
  `RollbackFailedError` is new. `DatabaseRestoreReport` drops
  `previous_database` and gains `database`, `administrator` and
  `dropped_tables`. The Run Journal's file format is unchanged; a journal an
  earlier version left is still read.
- Needs `l3io-wordpress-database` 0.3.5 (`drop_tables`, `load(...,
  prune_prefix=)`).

### Changed: root backups switch user, the library refuses root, short decryption passphrases warn (owner decisions 67-69, 2026-11-08)

- **A backup started as root runs as the Site Root's owner** (decision 67),
  as a restore does. It refuses a root-owned Site Root first (exit 2,
  `RootBackupRefusedError`), then reads the config file, the passphrase and
  the database credentials (the whole chain: flags, the environment, an AWS
  secret, `wp-config.php`), then switches to the owner's uid and gid (INFO
  `backup_runs_as_site_owner`). Backup and restore then share the owner's
  state directory and Site Lock, and the Archive is the owner's file. An
  Archive directory the owner cannot write is refused before the Run opens,
  a Dry Run too (exit 3, `ArchiveDirectoryNotWritableError`, naming the
  directory). v0.2.21 backed up as root and wrote a root-owned Archive.
  `wp-config.php` (read again for its settings and its copy in the Archive)
  and `--db-tls-ca` must be readable by the owner.
- **The library refuses a backup running as root** (decision 68):
  `backup()` raises `RootBackupRefusedError` before the Site is read, as
  `restore()` raises `RootRestoreRefusedError`; drop privileges to the Site
  Root's owner first, or use the `wpbackup2` command. `verify()` touches no
  Site and is not refused. `RootBackupRefusedError` and
  `ArchiveDirectoryNotWritableError` are new.
- **A decryption passphrase shorter than 4 characters is accepted**
  (decision 69): `verify --authenticate` and `restore` proceed with WARNING
  `credential_below_redaction_floor`, naming its source, never the value; it
  was refused (exit 2, `PassphraseUnavailableError`, "too short to
  protect"). A new encryption still needs 12 characters
  (`PassphraseTooShortError`, however short).

### Changed: unattended by default (owner decisions 54-65, 2026-11-08)

`wpbackup2 backup` and `wpbackup2 restore --archive latest` now run with no
flags, as a Docker sidecar or from cron. Pre-1.0 behaviour changes:

- **Every setting has a flag, an `L3IO_WP_BACKUP_*` variable and a config-file
  key** (owner decision 60, ADR-0028, Proposed). Precedence: flag, then
  variable, then file, then default. The file is TOML (`wp_dir = "..."`):
  `--config PATH` or `L3IO_WP_BACKUP_CONFIG`, else
  `/etc/l3io-wp-backup/config.toml` and `~/.config/l3io-wp-backup/config.toml`
  when present. A file holding a secret that its group or others can read is
  refused (exit 2, naming the file); an unknown key or a bad value is refused
  naming the key or the variable, never a value; an `L3IO_WP_BACKUP_*`
  variable no setting has is logged (`unknown_setting_variable`). `--wp-dir`
  and `--archive` are required as before, but a variable or a key satisfies
  them. New dependency: `pydantic-settings` (with pydantic).
- **New flags:** `--config` (every command), `--archive-dir` (backup,
  restore), `--passphrase` (backup, restore, verify), `--db-user` and
  `--db-password` (backup), `--db-tls` and `--db-tls-ca` (backup, restore),
  `--admin-user` (restore; `--admin-username` stays). A secret given as a flag
  is visible in the process list and logs WARNING `password_on_command_line`.
- **Removed:** `backup --confirm-db-host` (decision 54: the host is logged,
  `database_host`, and named in the Dry Run), `backup
  --allow-target-in-site-root` (decision 60: a target in the Site Root is
  written with WARNING `archive_target_in_site_root`) and `restore
  --allow-root` (decision 58, below). Each is now an unrecognised argument
  (exit 2).
- **Deprecated, still accepted:** `--admin-tls`/`--admin-tls-ca` are taken as
  `--db-tls`/`--db-tls-ca`; `--allow-unencrypted` has no effect. Each logs
  WARNING `deprecated_option`.
- **The default Archive is never inside the Site Root** (decision 60): it is
  written into `--archive-dir`, else the current directory when that is
  outside the site, else `~/.local/share/l3io-wp-backup/archives` (made
  `0700`). v0.2.21, run from the docroot, wrote it there.
- **No TLS is required by default** (decision 56), for backup or restore,
  the WordPress account or the administrator, as WordPress connects.
  `--db-tls {required,verify-ca,verify-identity}` opts in for every
  connection; `--db-tls-ca` needs a verifying mode. The earlier policy
  required verified TLS from a restore to any non-local host.
- **A restore started as root runs as the Site Root's owner** (decision 58):
  it reads the config, the passphrase, the password file and the Archive
  first, then switches to the owner's uid and gid. A root-owned Site Root is
  refused (exit 2). The library refuses a restore running as root.
- **`--force` only for a target that does not look like WordPress**
  (decision 57, files half): a non-empty Site Root holding `wp-config.php`,
  `wp-includes/` or `wp-content/` needs no flag. The database half arrives
  with the direct load.
- **A passphrase with an unencrypted Archive warns and proceeds**
  (decision 59, reversing the 2026-11-07 refusal): WARNING
  `archive_not_encrypted`; `--require-encrypted` is the hard stop.
- **An encrypted Archive's database is trusted on a bare server**
  (decision 65): no `--archive-database` is needed when the target has no
  `wp-config.php` and the Archive is encrypted; an unencrypted one still
  needs it.
- **`--archive latest`** (decision 64; also the variable and the key)
  restores the newest Archive of this site in the Archive directory, by
  Manifest `created_at`; "this site" is the Site Root and database the
  Manifest records. Logged (`archive_latest_selected`) and the first line of a
  Dry Run. A backup refuses `--archive latest`.
- **A database password shorter than 4 characters is accepted** (decision
  61), with WARNING `credential_below_redaction_floor`; it was refused (exit
  4).
- **Library:** `backup()` loses `confirm_db_host` and
  `allow_target_in_site_root` and gains `db_tls`; `restore()` loses
  `allow_root` and renames `admin_tls` to `db_tls`; `AdminTls` is now
  `DbTls`; `RestoreOutcome` gains `notes`. `CredentialTooShortError`,
  `DatabaseHostUnconfirmedError` and `TargetInsideSiteRootError` are gone;
  `NoArchiveForSiteError` and `PrivilegeDropError` are new.
- **`--overwrite-wp-config` asks `l3io-wordpress-config` 0.1.7** whether the
  Archive's `wp-config.php` declares `getenv_docker()` before the carried
  definitions (`declares_function`, now the floor), replacing backup's own
  textual check; its documented limits are in docs/restore.md.

### Added: the official Docker image's `getenv_docker()` settings (owner decisions 47, 48)

- **A site defining `DB_*` with `getenv_docker()` or `getenv()` is backed up.**
  The official `wordpress` image's `wp-config.php` reads `DB_HOST`, `DB_NAME`,
  `DB_USER`, `DB_PASSWORD` and `$table_prefix` from the container's
  environment. v0.2.21 used the expression's text as the value, so the host
  was `getenv_docker('WORDPRESS_DB_HOST', 'mysql')`. `backup`, `doctor` and a
  files-only `restore` now resolve them as PHP does, from the tool's own
  environment: the variable, a Docker secret named by `<NAME>_FILE`, else
  the default (through `l3io-wordpress-config` 0.1.5 or later). Run the
  tool with the site's `WORDPRESS_DB_*`; the resolved host is logged
  (`database_host`; `--confirm-db-host`, which named it, is removed above). A resolved password is redacted like any other, and the log
  names the variable (`config_value_from_environment`), never its value.
- **Any other PHP expression in those settings is refused**, exit 3
  (`REFUSED`, `UnsupportedConfigValueError`), naming the setting, never its
  text. v0.2.21 used the text as the value.
- **A database restore onto such a site is refused**, exit 3 (`REFUSED`,
  `EnvironmentDrivenTargetError`), before any change, a Dry Run included.
  v0.2.21 tried to reach the database at the expression's text (exit 5). How a
  database restore should switch such a site is proposed in ADR-0027 and not
  yet decided; see docs/restore.md.
- **`--overwrite-wp-config` keeps the target's `DB_*` definitions as written**
  (owner decision 48, BL-E008-032). v0.2.21 carried their values into the
  Archive's `wp-config.php`, so a `getenv_docker()` definition would have been
  written back as a fixed string. They are now carried as their PHP source
  (`l3io-wordpress-config` 0.1.6, now the floor): a lookup stays a lookup, and
  a literal lands as before unless the target spelled it other than
  `'value'`, when its own spelling is kept. When the target uses
  `getenv_docker()` and the Archive's `wp-config.php` does not define that
  function before those definitions, the restore is **refused**, exit 3
  (`REFUSED`, `UndefinedConfigFunctionError`), before any change, a Dry Run
  included: PHP would otherwise stop with "Call to undefined function". That
  check is textual, not a PHP parse; docs/restore.md lists what it can get
  wrong.

### Fixed

- **`L3IO_WP_BACKUP_ADMIN_USER` and `L3IO_WP_BACKUP_ADMIN_PASSWORD` alone
  supply the restore administrator**, as README documents. The password
  variable without `--admin-username` was a usage error (exit 2), also for
  `--mode files`, which needs no administrator. The variable fills in a
  missing `--admin-password` only when `--admin-username` is given.
- **A WordPress account with rights on its own database only is no longer
  refused** by the restore preflight (`missing_privilege`, exit 3). This is how
  the official `mysql` and `mariadb` images create `MYSQL_USER`. The
  administrator grants that account the staging database before the load, so
  those privileges are no reason to refuse. A load-check refusal now names the
  database and the account (`DatabaseRefusedError.database`, `.account`).

### Changed (AD-22 behaviour changes; owner decisions 2026-11-07)

- **A backup target inside the Site Root is refused** (BL-E002-064). v0.2.21
  wrote the Archive to the current directory, so running it from the docroot
  left a downloadable copy of the database in the Site Root. A backup whose
  target resolves inside the Site Root now exits 2 (`USAGE`) before any effect;
  the message names `--allow-target-in-site-root` and suggests a target outside
  the Site Root. The default name is unchanged; run from another directory or
  pass `--archive`.
- **A non-local `DB_HOST` must be confirmed** (BL-E002-063). A backup that
  dumps the database refuses a `DB_HOST` other than `localhost`, `127.0.0.1`,
  `::1` or a socket path, before any connection, exit 3 (`REFUSED`), unless
  `--confirm-db-host <host>` names it.
- **A restore running as root is refused** (BL-E002-098). It exits 2 (`USAGE`)
  unless `--allow-root` is given; with it, a WARNING `restore_running_as_root`
  names the extraction race.

### Changed exit codes (owner decision 2026-11-07, BL-E002-069/074/103)

- **An extraction refusal after the Site Root was cleared exits 8**
  (`VERIFICATION_FAILED`), never 3: the Site was changed, so it is not a
  no-effect refusal. The message says the Site was changed and names the
  Safety Snapshot (`ExtractionRefusedAfterCleanError`).
- **An encrypted Archive with no passphrase exits 4** (`CREDENTIALS`), as
  the exit-code table defines a missing key; it was 3 (`PassphraseRequiredError`).
- **A nonexistent `--wp-dir` or `--archive` exits 2** (`USAGE`); it was 1
  (`SiteRootNotFoundError`, `ArchiveNotFoundError`).

### Changed Archive encoding (owner decisions 2026-11-07)

- **New Archives are gzip streams** (BL-E002-032). The outer tar is written
  inside a gzip stream at compression level 0, so the `.tar.gz` name is true
  and `tar xzf` reads it; the members, their order and `format_version` 1 are
  unchanged. Archives written before, plain tars, stay readable permanently.
- **`created_at` is written `2026-11-07T21:04:05Z`** (BL-E002-005): UTC, whole
  seconds, a `Z` suffix, the spelling log records use. The earlier
  `+00:00`/fractional form is still read.

### Changed restore behaviour (owner decisions 2026-11-07)

- **`--force` no longer means "clean everything", and `--cleanfirst` no longer
  removes the database** (AD-25, B-3; SD-02-10). In v0.2.21 `--force` selected
  the `ALLCLEAN` behaviour and `--cleanfirst` also dropped the database. Now
  each destructive effect needs its own flag: `--cleanfirst` clears the Site
  Root only, `--drop-database` removes the database that loses the cutover,
  and `--force` only waives the non-empty-target refusal for the parts the
  Mode restores — it selects no Mode and authorises no removal. A script that
  relied on `--force` or `--cleanfirst` to remove the database must add
  `--drop-database`.

- **A failed load check is exit 7** (BL-E002-104). The database's load
  readiness check runs as the WordPress account the load runs as and no longer
  needs administrator credentials; a check that cannot run is
  `LoadReadinessError` (`NOT_READY`), naming that account's credential source.
- **`--overwrite-wp-config` refuses an Archive config that defines a key
  twice** (BL-E002-091): the replace is now `l3io-wordpress-config`'s
  `replace_content`, which refuses such a file (PHP keeps the first definition).

### Changed behaviour, close-out batch (owner decisions 35 and 39, 2026-11-07)

- **Ctrl-C exits 130 with one line, no traceback** (L3). The interrupted
  Run's `run_finished` and the `command_failed` record carry `exit_code` 130
  and `exit_reason` `INTERRUPTED`; they were `null`.
- **A per-user state directory another user could tamper with is refused**
  (BL-E008-027). The state directory and everything the Site Lock, the Run
  Journal and WP-CLI use under it must be real directories (not symlinks),
  owned by you (the effective user) and not group- or world-writable; a lock
  directory that is not exits 7 (`NOT_READY`, `SiteLockLocationError`). It
  was used as found.
- **The library refuses a wrong-typed closed choice as `UsageError`**
  (BL-E006-018): `scope`, `mode`, `overrides`, `snapshot` or `config_policy`
  of the wrong type (a string such as `"all"`) names the parameter, exit 2,
  before anything runs. It surfaced as `UnexpectedError`, a defect.
- **An `on_warning` callback that raises after the Safety Snapshot no longer
  aborts the restore** (BL-E008-022): it is logged (ERROR
  `on_warning_failed`) and the restore continues. A notice before any effect
  still propagates the callback's exception.
- **`--allow-unencrypted` with no effect is noted** (BL-E008-021): INFO
  `restore_flag_no_effect` when the Archive is encrypted, no passphrase was
  given, or `--require-encrypted` refuses the plain Archive anyway. Never a
  refusal.
- **A `wp-config.php` laid down by the tree extraction loses its "other"
  permissions** (BL-E008-023), as one laid down by role already did, before
  the relocated database password is written into it: `0644` becomes `0640`,
  never `0600`, so a web server reading it through a shared group still can
  (owner decision 43).
- **Restore's `CLEAN` keeps a name case-insensitively** (RT-005): on a
  case-insensitive filesystem `WP-Config.php` is the kept `wp-config.php`.
- **`wp` and the tool probes get only the absolute `PATH` entries**
  (BL-E007-013): the ones `wp` was located over, so `php` is found by the same
  rule.
- **Extraction holds every member to its indexed size** (BL-E003-088), those
  of roles not restored included, so a forged header cannot make the restore
  decompress more than the Archive lists.
- **`doctor` says what stopped the database check** (BL-E008-020): a
  `blocker_details` entry (JSON) or `database blocker:` line names the error
  and its message, beside the exit-code name.
- **Help**: every subcommand's `--help` has a description and the exit-code
  table; `--log-level` shows its choices; help lines share one style
  (UX-01-11). `--whatIf` says a Dry Run takes the Site Lock (AD-5). A
  passphrase refusal from the CLI no longer suggests `passphrase=` (UX-01-17).
- **Durability**: a published Archive's directory is fsynced after the rename
  (AR-10; WARNING `archive_directory_not_synced` if it cannot be).

### Changed behaviour, close-out batch (owner decisions 40 to 44, 2026-11-07)

- **A restore Dry Run lists the symlinks the real Run would skip** (owner
  decision 41, BL-E003-021): after its actions, one `would skip symlink` line
  per absolute or escaping Site Root link, with the reason; for a v0 Archive,
  which records no link targets, one line saying they are reported at
  extraction. Under `--log-format json` each `skipped_links` entry now carries
  `reason` (`absolute` or `outside_destination`), real Run and Dry Run alike;
  a Dry Run's list is the planned one, and is `null` for a v0 Archive. For a
  library caller, `RestoreOutcome.skipped_links` is `None` in that one case.
- **State-directory ownership is judged by the effective uid** (owner decision
  44, BL-E008-027): the state-directory check and the stale-workspace sweep
  compare with `os.geteuid()`, the owner of what the Run creates, not the real
  uid. Under `sudo -E` a state directory the invoking user owns is refused,
  naming both uids and `XDG_STATE_HOME`.
- **Documented**: the restore space and expansion checks are advisory (they
  trust the unverified index; verification and extraction enforce), `CLEAN`'s
  Site Root check is a guard against a mistyped `--wp-dir`, not proof of a
  WordPress Site (ADR-0022 amendment), and `docs/restore.md` defines its terms.

### Fixed, close-out batch (owner decision 46, 2026-11-08)

- **A v0 Archive's `wp-config.php` counts for the database cutover**
  (BL-E004-036). A v0 Archive keeps its config in its tree
  (`wp-root/wp-config.php`). Its `config_path` now names the location,
  `wp-config.php`, as a v1 Archive with an in-tree config records it, and no
  longer the member path. So a restore in `all` or `all-clean` Mode onto a
  target with no `wp-config.php` uses the Archive's copy for the cutover when
  nothing else lays a config down, as it already did for v1. Before, the plan
  was refused with `NoConfigForCutoverError` (exit 3). `wpbackup2 restore` and
  the library's `restore()` were not affected: they lay the v0 config down
  before the plan is built. The refusal reached code that builds the restore
  plan directly, with no relocation.

### Changed library surface (owner decision 36, 2026-11-07)

- **`UsageError.arguments` names library parameters** (BL-E006-015). On every
  path a library call reaches, `arguments` is the parameter's name, dotted for
  a field of one: `allow_unencrypted`, `require_encrypted`, `authenticate`,
  `allow_root`, `config_policy`, `archive_database`, `admin_tls` and
  `admin_tls.ca_file`, `relocation.new_site_url` (and the other
  `relocation.*` fields), `target` and `allow_target_in_site_root`, `archive`
  or `target` for an unusable Archive location, `patterns` for an unusable
  exclusion pattern. It was the CLI flag (`--allow-unencrypted`,
  `--admin-tls-ca`, ...) for these. Only the command line's own argument
  checks still name flags. Messages are unchanged.
- **`AdminTls` is defined in `ports/database.py`** (BL-E003-099); import it
  from `l3io.wp.backup.api` as before.

### Changed log fields (owner decisions 2026-11-07)

- Every record a Run emits carries `l3io_run_id` and `l3io_dry_run` instead of
  `run_id` and `dry_run` (BL-E006-019); `command_failed` names the failed Run
  in `l3io_run_id`. `run_started` and `run_finished` keep their own `run_id`.
- A restore's `phase` field is upper case with underscores (`LOAD_DUMP`,
  `REWRITE_CONFIG`), as a backup's is (BL-E002-109). The Run Journal's
  on-disk spelling is unchanged.
