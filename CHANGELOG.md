# Changelog

Behaviour changes an operator of `wpbackup2` v0.2.21 must know about. The
release notes proper are built from commit subjects
(`.github/changelog-config.json`); this file carries the named entries the
owner asked for, which a commit subject cannot hold.

## Unreleased

### Fixed: image builds wait for PyPI's CDN (1.1.2)

- **The 1.1.1 image builds failed** with "there is no version of
  l3io-wordpress-backup==1.1.1" about a minute after the PyPI upload, and the
  MariaDB variant again on a rerun minutes later (docker.yml run
  43501774937): PyPI's CDN served those runners a project page from before the
  upload. The workflow's wait had passed, because it checked with `pip
  download` while the build installs with `uv`.
- **One install, used twice.** `docker/install-release` is now the only way
  the image installs the release, and docker.yml's `resolve` job waits by
  running the same script with `--dry-run`, with the uv the Dockerfile pins,
  no cache and `--refresh`, until it succeeds three times in a row (at most
  20 minutes).
- **The build retries, bounded.** A "no such version" answer is retried with
  backoff (10 s doubling to 60 s, each retry logged) for at most 10 minutes;
  any other failure fails at once. A version that does not exist still fails,
  naming the version and the bound.
- The `latest` decision now waits until PyPI's JSON API lists the release, so
  a stale page cannot leave the floating tags behind while the run succeeds.
- Nothing changes in the image or in `wpbackup2`.

## 1.1.1 — 2026-11-13

### Changed: faster backups and Safety Snapshots; new setting `compression_level` (owner decision 88)

- **The payload is gzip level 6 by default, not 9.** The inner payload was
  written by Python's `tarfile` at its implicit level 9, which nobody had
  chosen; it is now level 6. On a 1.1 GiB test site (WordPress core five
  times, a 150 MiB SQL-like file, 406 MiB of incompressible media), writing
  the Archive took 52-54 s instead of 88 s (1.7 times faster) for 0.2% more
  bytes. A restore's Safety Snapshot, written the same way, gains the same.
- **New setting `compression_level`**: `--compression-level N`,
  `L3IO_WP_BACKUP_COMPRESSION_LEVEL` and the `compression_level` config key,
  on `backup` (the Archive) and `restore` (its Safety Snapshot); the
  library's `backup(compression_level=)` and `restore(compression_level=)`.
  A whole number from 1 (fastest: 2.7 times faster than 9 on the site above,
  5.6% more bytes) to 9 (smallest, the old behaviour). Any other value is a
  usage error (exit 2) before the Run starts. The level is logged on
  `backup_started` and the `WRITE_ARCHIVE` `planned_action`, and the Dry
  Run's `write Archive` line names it.
- **Unchanged:** the Archive format (`format_version` 1; nothing records the
  level, and every version reads every level), the outer stream's level 0
  (decision 21), and the order: the payload is compressed, then encrypted.
  The image passes `L3IO_WP_BACKUP_COMPRESSION_LEVEL` through as is.

### Changed: the image never changes ownership; it runs as the user it is given (owner decision 87)

- **No ownership changes at start.** The image no longer re-owns anything:
  the scheduled mode's `lsiown -R` of `XDG_STATE_HOME`, `TMPDIR`, `HOME` and
  the top of the Archive directory, and the one-shot mode's same changes for
  `PUID:PGID`, are gone. A one-shot with another `PUID` used to re-own a
  shared state volume (Safety Snapshots, journals, locks), and the next normal
  Run then failed (exit 7, `SiteLockLocationError`). **Operators must own their
  mounts**: the Archive and state folders must be the user's the image runs as.
  New named volumes are built `33:33 0700`, the default user's, so the default
  setup needs nothing; a volume or bind mount of another owner needs a
  `chown -R` by the operator, once (docs/docker.md, Ownership).
- **Which user.** Started as root: `PUID:PGID` (33:33 when unset) for both
  modes, one-shot and scheduled (Supercronic and every job), with `setpriv`
  (no longer the base's `abc` user); `PUID=0` stays root, and `wpbackup2` runs
  a backup, restore or prune as the Site Root's owner itself (owner decisions
  58, 67). Started with Docker's `--user` (compose `user:`): that user, **in
  the scheduled mode too** (refused before), without s6; add `init: true`.
  `PUID`/`PGID` naming another user under `--user` are refused (exit 2): a
  container not started as root cannot switch.
- **`PUID`/`PGID` are no longer set in the image's environment** (unset means
  33:33 as before), so a `--user` container can tell that no other user was
  asked for.
- **Refused up front, never fixed.** The scheduled mode checks, at start and
  as the jobs' user, the state directory, `TMPDIR` and the Archive directory
  (write) and the Site Root and its `wp-config.php` (read); a folder it cannot
  use stops the container (exit 3, or s6's stop when started as root) with the
  path, the uid and gid, the owner, group and mode found, and the fix, rather
  than failing every night.
- **`wpbackup2` checks the same before every Run** (any install, not only the
  image), a Dry Run too: the state directory (`backup`, `restore`, `prune`),
  `TMPDIR` when set (`backup`, `restore`; Python would otherwise fall back to
  another temporary directory silently), the Archive directory (`backup`,
  `prune`), a backup's Site Root, a restore's `--snapshot-dir`, and the
  Archive a `restore` or `verify` opens. Refused with the new
  `PathNotAccessibleError`, **exit 3**, whose message names the path, the
  effective `uid:gid`, the owner, group and mode, and the fix, and says that
  wpbackup2 never changes ownership. Before, the same cases failed later and
  less clearly: an unwritable state directory as exit 7
  (`SiteLockLocationError`), an Archive the user may not read as exit 2
  (`ArchiveNotFoundError`, "does not exist or is unreadable").
- **The image's own folders need no chown:** `TMPDIR` (`/tmp/wpbackup`) is
  built `1777` (sticky, like `/tmp`), and the scheduled crontab moved into it
  (`wpbackup-schedule-<uid>/`, `0700`; it was `/run/wpbackup`); `HOME` is
  root's and read-only (nothing writes it). A container started with `--user`
  runs on a read-only root filesystem in both modes, with a tmpfs at
  `/tmp/wpbackup`.
- **What the base image still does** (linuxserver's s6 init, root starts of
  the scheduled mode only): re-numbers its `abc` user and re-owns the top of
  `/app`, `/config` and `/defaults`, which this image does not use; do not
  mount anything there.

### Fixed: the docs' unwritable-restore-target example

- A restore as a user that is not the Site Root's owner exits **4**
  (`CREDENTIALS`), not 3, when that user cannot read the site's
  `wp-config.php` (a `0640` file of the owner's): the config is read first.
  Exit 3 (`TargetNotWritableError`) needs a user that can read the config
  and the Archive but not write the Site Root. docs/docker.md and
  docs/restore.md now say so; `PUID=0` remains the fix.

## 1.1.0 — 2026-11-12

### Changed: the default Archive name is `wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz` (owner decision 82)

- **The name a backup gives an Archive without `--archive`** was
  `wpbackup2-<db>-<YYYYmmddHHMMSS>.tar.gz`; it is now
  `wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz` (UTC, ISO-style, so one site's
  Archives sort by time). Scripts that match the old name must match the new
  one too.
- **`<site>` is the new setting `site_name`**: `--site-name`,
  `L3IO_WP_BACKUP_SITE_NAME`, or `site_name` in the config file; without it,
  the database name. Characters outside `A-Z a-z 0-9 . _ -` become `-`; a value
  with none left is refused (exit 2).
- **Old Archives keep working.** `restore --archive latest` chooses by each
  Archive's Manifest, never its name, so `wpbackup2-*` and `wpbak-*` Archives
  of a site are found alike, and another site's never. Both names are left out
  of a backup of a Site Root that holds them, and the stale `.partial` sweep
  removes a killed Run's staged file under either. `--archive` still names any
  Archive. The Manifest is unchanged: it already records the site (its root and
  database), and the name is only a label.

### Added: `wpbackup2 prune --keep N`, and pruning in the image (owner decision 83)

- **`wpbackup2 prune --keep N`** deletes this site's older Archives in the
  Archive directory, keeping the newest N (at least 1) by Manifest
  `created_at`. "This site's" is the matching `--archive latest` uses, so
  `wpbak-*`, legacy `wpbackup2-*` and renamed Archives count alike. Never
  deleted: the newest Archive, Safety Snapshots, another site's Archives,
  files whose Manifest cannot be read, symlinks and `.partial` files.
  `--keep` is required (also `L3IO_WP_BACKUP_KEEP`, config key `keep`). It
  takes the Site Lock, removes each file only if it is still the one read,
  logs `archive_pruned` per file and `prune_completed`, and `--whatIf` lists
  without deleting. Started as root it runs as the Site Root's owner.
- **`api.prune(site, keep=..., archive_dir=None, dry_run=False)`** returns a
  `PruneResult`; `PruneFailedError` (exit 1) when a chosen Archive could not
  be removed.
- **The image prunes after each successful scheduled backup**, with the
  tiered defaults below (`WPBACKUP_KEEP` keeps the newest N when set; a value
  that is not a whole number stops the container at start). One-shot runs
  prune only when asked.

### Added: tiered retention in `prune` (owner decision 84)

- **`--keep-daily N`, `--keep-weekly N`, `--keep-monthly N`,
  `--keep-yearly N`**: each keeps the newest of this site's Archives per UTC
  day, ISO week (Monday to Sunday; a week across 31 December is one week),
  month or year, over the last N of them that have an Archive. They combine
  with `--keep N` and with each other: an Archive kept by any rule stays, the
  newest always. At least one rule is required (exit 2 otherwise); each is at
  least 1. Nothing is copied: a nightly Archive that is the last of its week
  and month stays as the weekly and the monthly after the daily window passes
  ([docs/docker.md](docs/docker.md#how-retention-works)).
- **Three spellings each**: `L3IO_WP_BACKUP_KEEP_DAILY` (and `_WEEKLY`,
  `_MONTHLY`, `_YEARLY`), config keys `keep_daily` and so on;
  `api.prune(..., keep_daily=..., keep_weekly=..., keep_monthly=...,
  keep_yearly=...)`, and `keep` is now optional there too.
- **What a prune says**: `archive_pruned` carries `reasons` (why each rule
  lets the Archive go), `archive_kept` (DEBUG) the rules that keep each one,
  and `prune_completed` the `rules` and both lists. stdout lists each Archive
  kept with its rules, each deleted with why, then the totals.
  `PruneResult.keep` is now `policy` (an `api.RetentionPolicy`), `kept` holds
  `api.KeptArchive` values, and `PrunedArchive` has `reasons`.
- **The image's default retention is 7 daily, 4 weekly, 6 monthly**
  (`WPBACKUP_KEEP_DAILY=7`, `WPBACKUP_KEEP_WEEKLY=4`,
  `WPBACKUP_KEEP_MONTHLY=6`; `WPBACKUP_KEEP_YEARLY` unset), replacing the flat
  `WPBACKUP_KEEP=14`, which is now unset by default and still keeps the
  newest N when set. The scheduled job passes only the rules set and not `0`;
  all unset or `0` disables pruning.
- The bucketing is this package's own (no maintained library fits):
  [ADR-0029](docs/adr/0029-tiered-retention-bucketing.md) records the
  libraries evaluated.

### Added: optional Archive subfolders (owner decision 86)

- **`--archive-subdir TEMPLATE`** (`L3IO_WP_BACKUP_ARCHIVE_SUBDIR`, config key
  `archive_subdir`) puts the default-named Archive in a subfolder of the
  Archive directory: a relative path using `{site}` (the name's `<site>`),
  `{yyyy}`, `{mm}` and `{dd}` (the UTC date of the name's own timestamp, so
  folder and name always agree), such as `{site}/{yyyy}/{mm}`. Missing folders
  are made `0700`, by the Run's user (in the image, `PUID`). Unset (the
  default), the Archive directory stays flat, as before. `--archive` still
  names the file outright. The Dry Run shows the resolved path and makes no
  folder.
- **Refused with exit 2, from every spelling, before any Run:** an unknown
  token, a format spec or conversion, a literal or unbalanced brace, an
  absolute path, a `.`, `..` or empty segment, a character outside
  `A-Z a-z 0-9 . _ - /`, more than 6 levels; and, per backup, a `{site}` that
  would make a `.`/`..` folder or a folder on the way that is a symlink or a
  file.
- **`restore --archive latest` and `prune` search subfolders** of the Archive
  directory, up to 6 levels, never through a symlinked folder, skipping Run
  workspaces and the state directory, and still match by Manifest: flat and
  nested Archives, under either name, are one site's set. `latest` now reads
  the same list `prune` does, so a symlink named `*.tar.gz` is no longer a
  `latest` candidate (it never was a `prune` one).
- **`prune` removes the subfolders it empties** (`archive_dir_removed`;
  `PruneResult.removed_directories`; a `removed empty folder` line on stdout):
  only folders a removed Archive was in, never the Archive directory, a folder
  still holding anything, or a symlink; none in a Dry Run.
- **The stale `.partial` sweep** reaches every subfolder when
  `--archive-subdir` is set.

### Added: a MariaDB-client image variant (owner decision 81)

- **`:X.Y.Z-mariadb`, `:X.Y-mariadb`, `:X-mariadb`, `:mariadb`**, built with
  `DB_CLIENT=mariadb` beside the default (MySQL client) image, on GHCR and
  Docker Hub. For MariaDB servers: its `mariadb-dump` logs no
  `client_server_skew`, which the default image's MySQL 8.4 `mysqldump` logs on
  every dump of a MariaDB server. It does not work against MySQL (8.0 and 8.4:
  the dump exits 2); use the default image there. The default image is
  unchanged.
- Each variant has its own scan, SBOM and provenance, sidecar suite (the
  `-mariadb` one on MariaDB), attestation and anonymous-pull check.
  [docs/docker.md](docs/docker.md#which-variant) says which to pick.

### Changed: a restore its user cannot write is refused before any effect (Phase 3)

- **`TargetNotWritableError`, exit 3**, a Dry Run too: the target survey
  checks that the user the Run runs as can write the Site Root, the existing
  directories and files the Archive's files go into or replace, every
  directory `--cleanfirst` clears, and where a laid-down `wp-config.php` goes,
  and names what it cannot and the uid. Before, such a restore passed
  `PREFLIGHT` and `VERIFY`, failed at `EXTRACT` with "could not write archive
  '/var/www/html'", and left an interrupted Run needing
  `--resolve-interrupted-run`. `--force` does not waive it.
- **The extraction's own write failure names the Site Root**
  (`ExtractionWriteError`, a subclass of `ArchiveWriteError`, exit 1
  unchanged): "could not write into the Site Root '<path>'", not "could not
  write archive".
- **The undo text says why there is no Safety Snapshot**: "not needed: the
  target was empty" or "opted out (--no-safety-snapshot)", recorded in the Run
  Journal (`snapshot_skipped`). It said "opted out" whatever the reason. A
  journal an earlier version wrote still reads, and says "none was taken".
- **Docs:** restoring an Archive another uid wrote (the usual disaster-recovery
  case) is a restore started as root, `PUID=0` in the image; the command then
  runs as the Site Root's owner ([docs/restore.md](docs/restore.md#running-as-root),
  [docs/docker.md](docs/docker.md#restoring-an-archive-another-user-wrote)).

### Fixed: a database warning's log record has an event name

- `l3io.wp.database` logs a warning finding (such as `client_server_skew`)
  with its text as `event`. `wpbackup2` now logs it as `event`
  `database_warning`, the finding's `code` kept and the text in `message`, so
  a log query can select on it ([docs/logging.md](docs/logging.md)).

### Added: the official Docker image (owner decisions 77–80)

- **`ghcr.io/liquidlogiclabs/l3io-wordpress-backup`** and
  **`docker.io/ravensorb/l3io-wordpress-backup`**, public,
  `linux/amd64` and `linux/arm64`, built from the PyPI release by
  `.github/workflows/docker.yml` after each PyPI publish. The first image is
  1.0.0, built from the 1.0.0 package on PyPI; the package itself is unchanged.
- **A sidecar for the official `wordpress` image**: `L3IO_WP_BACKUP_WP_DIR`
  `/var/www/html`, `L3IO_WP_BACKUP_ARCHIVE_DIR` `/backups`, runs as uid/gid 33
  (`PUID`/`PGID`). One image for MySQL 8.0/8.4 and MariaDB 11.4 servers (MySQL's
  8.4 client).
- **Scheduled by default**: Supercronic under s6 runs `wpbackup2 backup` on
  `WPBACKUP_SCHEDULE` (default `0 3 * * *`) plus an optional mounted crontab.
  Given a command it runs once: `docker run IMAGE restore --archive latest`.
- **No WP-CLI in the image**: a `--new-site-url`/`--new-site-home-url`
  relocation stops before any change with exit 7 (`NOT_READY`).
- Every release is scanned (fixable CRITICAL/HIGH fails it), carries an SBOM
  and provenance, and is tested with the sidecar suite on the pushed digest
  before it is tagged. See [docs/docker.md](docs/docker.md).

## 1.0.0 — 2026-11-10

### Changed: the family libraries are required dependencies; `[database]` is now an empty alias (2026-11-10)

- **`pip install l3io-wordpress-backup` backs up and restores** (owner decision
  76). `l3io-wordpress-database[encryption]` (>=0.3.5), `l3io-wordpress-config`
  (>=0.1.7) and the `l3io-age-passphrase` binder (>=0.1.1) moved from the
  `database` extra into the required dependencies.
- **`[database]` still installs**, without a warning: the extra is kept and is
  empty, so `pip install 'l3io-wordpress-backup[database]'` installs exactly
  what the plain install does.
- **`aws` stays optional** (decision 71): `pip install
  'l3io-wordpress-backup[aws]'` adds boto3, and the base install pulls none.
- **A broken installation says so**: one that lacks a required dependency
  anyway (installed with `--no-deps`, or pruned afterwards) raises
  `IncompleteInstallError` (new; exit 7, `NOT_READY`, naming the missing
  distribution) — "the installation is incomplete ...; reinstall
  l3io-wordpress-backup". It used to be `MissingExtraError` naming the
  `database` extra; `MissingExtraError` now names only an optional extra
  (`aws`).
- On a platform with no published binder wheel, the binder's source build
  (which needs a Rust toolchain) now decides whether the package installs,
  where before only the extra failed.

### Changed: the family resolves from PyPI (2026-11-10)

- `pip install l3io-wordpress-backup` installs from PyPI alone, with
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
