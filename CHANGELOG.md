# Changelog

Behaviour changes an operator of `wpbackup2` v0.2.21 must know about. The
release notes proper are built from commit subjects
(`.github/changelog-config.json`); this file carries the named entries the
owner asked for, which a commit subject cannot hold.

## Unreleased

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

- **A failed load check is exit 7** (BL-E002-104). The database's load
  readiness check runs as the WordPress account the load runs as and no longer
  needs administrator credentials; a check that cannot run is
  `LoadReadinessError` (`NOT_READY`), naming that account's credential source.
- **`--overwrite-wp-config` refuses an Archive config that defines a key
  twice** (BL-E002-091): the replace is now `l3io-wordpress-config`'s
  `replace_content`, which refuses such a file (PHP keeps the first definition).

### Changed log fields (owner decisions 2026-11-07)

- Every record a Run emits carries `l3io_run_id` and `l3io_dry_run` instead of
  `run_id` and `dry_run` (BL-E006-019); `command_failed` names the failed Run
  in `l3io_run_id`. `run_started` and `run_finished` keep their own `run_id`.
- A restore's `phase` field is upper case with underscores (`LOAD_DUMP`,
  `REWRITE_CONFIG`), as a backup's is (BL-E002-109). The Run Journal's
  on-disk spelling is unchanged.
