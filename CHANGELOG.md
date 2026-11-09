# Changelog

Behaviour changes an operator of `wpbackup2` v0.2.21 must know about. The
release notes proper are built from commit subjects
(`.github/changelog-config.json`); this file carries the named entries the
owner asked for, which a commit subject cannot hold.

## Unreleased

### Added: the official Docker image's `getenv_docker()` settings (owner decisions 47, 48)

- **A site defining `DB_*` with `getenv_docker()` or `getenv()` is backed up.**
  The official `wordpress` image's `wp-config.php` reads `DB_HOST`, `DB_NAME`,
  `DB_USER`, `DB_PASSWORD` and `$table_prefix` from the container's
  environment. v0.2.21 used the expression's text as the value, so the host
  was `getenv_docker('WORDPRESS_DB_HOST', 'mysql')`. `backup`, `doctor` and a
  files-only `restore` now resolve them as PHP does, from the tool's own
  environment: the variable, a Docker secret named by `<NAME>_FILE`, else
  the default (through `l3io-wordpress-config` 0.1.5, now the floor). Run the
  tool with the site's `WORDPRESS_DB_*`; `--confirm-db-host` names the
  resolved host. A resolved password is redacted like any other, and the log
  names the variable (`config_value_from_environment`), never its value.
- **Any other PHP expression in those settings is refused**, exit 3
  (`REFUSED`, `UnsupportedConfigValueError`), naming the setting, never its
  text. v0.2.21 used the text as the value.
- **A database restore, or `--overwrite-wp-config`, onto such a site is
  refused**, exit 3 (`REFUSED`, `EnvironmentDrivenTargetError`), before any
  change, a Dry Run included. v0.2.21 tried to reach the database at the
  expression's text (exit 5), or with `--overwrite-wp-config` would have written
  the expressions back as fixed strings (BL-E008-032). How a database restore
  should switch such a site is proposed in ADR-0027 and not yet decided; see
  docs/restore.md.

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
