# Logging and Run correlation

`wpbackup2` writes one structured log stream to **stderr**. Every record a Run
emits — this package's, `l3io.wp.database`'s and `l3io.wp.config`'s — carries
the same `l3io_run_id`, so one Run can be pulled out of a week of logs:

```sh
wpbackup2 --log-format json backup --wp-dir /srv/www 2> backup.log
jq -c 'select(.l3io_run_id == "4b0e…")' backup.log
jq -c 'select(.event == "phase_completed") | {phase, duration_ms, bytes, exit_code}' backup.log
```

stdout carries only the command's result (the Archive path, or the verify
verdict), never a log record.

## Flags

| Flag | Values | Default |
|---|---|---|
| `--log-level` | stdlib level names, case-insensitive: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `--log-format` | `console`, `json` | `console` when stderr is a terminal, else `json` |

Both are top-level flags, given before the subcommand. They match
`l3io-wp-database` v0.2.0's CLI. v0.2.21's `--log-level` defaulted to
`CRITICAL`; the default is now `INFO` (triage E-14).

**Human-readable output off a terminal (cron mail).** Off a terminal stderr is
strict JSON lines by default, one object per line, warnings and failures
included: no plain-text line is mixed in, so a log collector can parse every
line (owner decision 2026-11-07, BL-E002-103 UX-5). Where a person reads the
output instead, such as a cron job whose output is mailed, pass
`--log-format console`:

```sh
# crontab: the mailed output is human-readable
0 3 * * * wpbackup2 --log-format console backup --wp-dir /srv/www --archive /var/backups/site.tar.gz
```

Under `console` the notices that `json` carries only as WARNING records (an
unauthenticated Archive, an unencrypted Safety Snapshot, a password on the
command line) are also printed as plain sentences.

## The record schema

This schema is part of the CLI contract (PRD §8). Log message wording is not.

<!-- schema:record -->
| Field | Type | On | Meaning |
|---|---|---|---|
| `l3io_run_id` | string (UUIDv4) | every record emitted inside a Run | The Run's identifier; random, so it leaks neither time nor path |
| `l3io_dry_run` | boolean | every record emitted inside a Run | `true` when the Run is a Dry Run (`--whatIf`): real queries, no-op commands |
| `timestamp` | string (ISO 8601, UTC, `Z`) | every record | When the record was emitted |
| `level` | string | every record | `debug`, `info`, `warning`, `error`, `critical` |
| `logger` | string | every record | The emitting logger, e.g. `l3io.wp.backup.adapters.phase`, `l3io.wp.database`, `l3io.wp.config.adapters.config_file` |
| `event` | string | every record | What happened, e.g. `run_started`, `phase_completed`, `run_finished` |
<!-- /schema:record -->

Every `phase_completed` record also carries exactly these phase fields, plus
any step-specific fields (verification adds `format_version`, `integrity`,
`dump_present`, `passed`, `authenticated`, and on failure `reason` and
`member`). `authenticated` is `false` for every keyless verification — the
checksums it checks live in the unprotected Manifest, so a pass means
consistent, not authentic — and `true` only when `verify --authenticate`
decrypted the whole payload with its passphrase:

<!-- schema:phase -->
| Field | Type | Meaning |
|---|---|---|
| `phase` | string | The step, upper case with underscores. A backup's: `DUMP`, `COLLECT_SITE_ROOT`, `ENCRYPT`, `WRITE_ARCHIVE`, `VERIFY`, `PUBLISH`; a restore's: `PREFLIGHT`, `VERIFY`, `SNAPSHOT`, `CLEAN`, `EXTRACT`, `REWRITE_CONFIG`, `LOAD_DUMP`, `SEARCH_REPLACE` (owner decision 2026-11-07, BL-E002-109; the Run Journal file keeps its own lower-case spelling) |
| `duration_ms` | number | Monotonic time the step took |
| `bytes` | integer | Bytes the step produced or read; `0` when not applicable |
| `exit_code` | integer | `0` on success; on failure the error's code from the [exit-code table](../README.md#exit-codes), the same one `run_finished` carries; `null` when the step was interrupted by `KeyboardInterrupt` (the Run's `run_finished` carries `130`, `INTERRUPTED`) |
| `outcome` | string | `ok` or `failed`; `skipped_dry_run` (with `bytes` `0`) for a step a Dry Run did not perform |
<!-- /schema:phase -->

`run_started` carries `operation` (`backup`, `verify`), `started_at` and the
package `version`; `run_finished` carries `exit_code` (the integer from the
[exit-code table](../README.md#exit-codes)), `exit_reason` (its name, such as
`VERIFICATION_FAILED`), `duration_ms` and, on failure, `error` (the error's
class name) — a verification that fails is a failure here too. A Run ended by
Ctrl-C (`KeyboardInterrupt`) carries `exit_code` `130` and `exit_reason`
`INTERRUPTED`: the shell's convention, which is not in the table; the command
exits 130 and prints one line, no traceback. They are the first and last
records of every Run.

**A warning from the database library.** `l3io.wp.database` reports what a
check found as findings, each with a stable `code`. A warning finding (one
that does not stop the Run, such as `client_server_skew`: the dump client is
older than the server or of the other family) is logged at `warning` with
`event` `database_warning`, the finding's `code`, its text in `message`, and
`operation` and `database`. Select on the code, never on the text:

```sh
jq -c 'select(.event == "database_warning") | {code, message, operation}' backup.log
```

The library itself (0.3.5) puts the text in `event`; `wpbackup2`'s logging
renames the record (`cli/logging.name_database_warnings`), so a Python caller
that configures its own logging sees the library's form.

Before preflight, a backup logs one `planned_action` record per action it
plans, in order — `ordinal`, `step`, the rendered `action` line and the step's
parameters (the collection line adds `exclusion_source` and
`exclusion_patterns`) — in a real Run and in a Dry Run alike.

## Failures

A failed command always leaves one `command_failed` record at `error`, naming
the error class in `error`, the operator message in `message`, and the code in
`exit_code` and `exit_reason` — an Archive that did not verify included
(`VerificationFailedError`, exit 8). An unexpected exception (a defect) also
carries its traceback, redacted like every other field. This holds for a failure
before any Run opened (a missing `--wp-dir`, a bad `--archive`), which has no
`run_finished` and no `l3io_run_id`. It does not hold for command-line usage errors
(exit 2): argparse prints plain text before logging is configured. With `--log-format console` the plain `wpbackup2: ...` line is
printed to stderr as well; with `--log-format json` it is not, so stderr stays
one JSON object per line and `jq` over it never stops at a failed Run.

## Events

Every event the package logs, by its `event` name. `tests/contracts/test_log_events_documented.py`
derives the emitted set from the source and fails on an event without a row
here, or a row naming an event the package no longer emits. Fields beyond the
[record schema](#the-record-schema) are named where they matter; credentials
never appear in any of them.

| Event | Level | When |
|---|---|---|
| `archive_bound` | INFO | A restore copied the Archive into the Run workspace before reading it (`archive`, `copy`) |
| `archive_database_trusted` | INFO | A config-less restore target uses an encrypted Archive's own database, authenticated by its passphrase, without `--archive-database` (`db_host`, `db_port`, `db_name`, `db_user`, `reason`; owner decision 65) |
| `archive_database_confirmed` | INFO | A config-less restore target matched `--archive-database` against the Archive's own database identity (`db_host`, `db_port`, `db_name`, `db_user`) |
| `archive_database_unused` | INFO | `--archive-database` was given but the target has its own `wp-config.php`, so it was not used |
| `archive_directory_not_synced` | WARNING | The destination directory could not be fsynced after the Archive was renamed into place; the Archive is published (`path`, `reason`) |
| `archive_latest_selected` | INFO | `--archive latest` chose this site's newest Archive (`archive`, `created_at`, `archive_dir`, `considered`, `matched`; owner decision 64) |
| `archive_kept` | DEBUG | `prune` keeps one of this site's Archives (`path`, `created_at`, `reasons`: the rules that keep it, of `newest`, `daily`, `weekly`, `monthly`, `yearly`; owner decision 84) |
| `archive_pruned` | INFO | `prune` removed one of this site's Archives, or in a Dry Run would (`path`, `bytes`, `created_at`, `performed`, `reasons`: why each rule given lets it go; owner decisions 83, 84) |
| `archive_prune_failed` | WARNING | `prune` chose an Archive and left it in place: it changed after it was read, or could not be removed (`path`, `reason`); the command then exits 1 |
| `archive_prune_skipped` | INFO | `prune` found a `*.tar.gz` entry that is not a regular file (a symlink, a directory) and did not consider it (`path`, `reason`) |
| `prune_completed` | INFO | A prune finished: `archive_dir`, `rules` (each rule given, by setting name), `kept`, `pruned`, `bytes`, `skipped`, `considered`, `matched`, `performed`, and `kept_archives` / `pruned_archives` (each `path`, `created_at`, `reasons`) |
| `archive_manifest_unread` | DEBUG | `--archive latest` could not read a file's leading Manifest and skips it (`path`, `error`, the class name) |
| `archive_not_encrypted` | WARNING | A restore was given a passphrase but the Archive is not encrypted; it proceeds (`archive`, `reason`; `--require-encrypted` refuses instead; owner decision 59) |
| `archive_discarded` | INFO | A staged Archive was removed without being published (`path`, its final name) |
| `archive_published` | INFO | A verified Archive was renamed onto its final name (`path`) |
| `archive_read_stopped` | DEBUG | Reading the outer tar stopped at a stream error; the classifier decides truncated or unrecognised (`error`, the class name) |
| `archive_staged` | DEBUG | A temporary file was created beside the final Archive name (`path`, `staged`) |
| `archive_target_chosen` | INFO | Where a backup writes its Archive, and what chose it: `--archive`, its variable or config key, `--archive-dir`, the current directory or the per-user data directory (`path`, `chosen_by`; owner decision 60) |
| `archive_target_in_site_root` | WARNING | A backup writes its Archive inside the Site Root, where the web server may serve it; it proceeds (`path`, `site_root`; owner decision 60) |
| `archive_unauthenticated` | WARNING | A restore proceeds from an unencrypted Archive, whose checksums prove consistency, not authenticity (`integrity`, `archive`) |
| `backup_completed` | INFO | A backup finished (`path`, `format_version`, `members` by kind, `bytes`, `performed` — `false` in a Dry Run) |
| `backup_started` | INFO | A backup's plan begins executing (`backup_scope`) |
| `command_failed` | ERROR | A command failed, or was interrupted by Ctrl-C (`exit_code` `130`, `exit_reason` `INTERRUPTED`); see [Failures](#failures) (`error`, `message`, `exit_code`, `exit_reason`) |
| `config_files_read` | INFO | The settings files read, in order, before the Run (`paths`; owner decision 60) |
| `config_directory_not_synced` | WARNING | The directory of a written `wp-config.php` could not be fsynced after the rename; the file has changed (`path`, `reason`) |
| `config_laid_down` | INFO | A restore wrote `wp-config.php` from the Archive into a target that had none (`config_path`, `mode`) |
| `config_located` | INFO | A relocating restore chose where WP-CLI reads `wp-config.php` (`config_path`, `location`, `action`, `source`, `manifest_config_path`) |
| `config_mode_restricted` | WARNING | A laid-down `wp-config.php` was world-accessible in the Archive; its other-user bits were removed (`config_path`, `archive_mode`, `mode`) |
| `config_value_from_environment` | INFO | A `wp-config.php` setting defined with `getenv_docker()` or `getenv()` was resolved from the Run's environment, or the file a `NAME_FILE` variable names (`path`, `key`, `variable`, `function`; never the value) |
| `config_written` | INFO | `wp-config.php` was changed: `method` `set_constants`, `save_copy` or `replace_file` (`path`, `keys`, `changed`, never values) |
| `credential_below_redaction_floor` | WARNING | A database password, or a passphrase that decrypts (verify, restore), shorter than the redaction floor was accepted; it cannot be masked in logs (`source`, `minimum`; never the value; owner decisions 61, 69) |
| `credential_chain_built` | DEBUG | The credential sources for a `role` were ordered (`sources`; `none_configured` when there are none) |
| `credential_source_empty` | WARNING | A credential source was set but empty and was skipped (`source`, a name, never a value) |
| `credential_source_selected` | INFO | The database password was resolved from `credential_source` (logged when the source changes) |
| `cutover_abandoned` | WARNING | Resolving an interrupted Run found `wp-config.php` back at its pre-run values, so the cutover is not completed (`load_database`, `database_exists`, `reason`) |
| `cutover_completed` | INFO | `--resolve-interrupted-run` wrote the cutover keys an interrupted Run left pending (`keys`, `wp_config_path`) |
| `cutover_key_written` | DEBUG | One cutover key was written to `wp-config.php` (`key`, `wp_config_path`) |
| `cutover_read_back_failed` | ERROR | After a cutover write failed, reading the key back failed too; the write's error is raised (`key`, `error_type`) |
| `cutover_write_error_landed` | WARNING | A cutover write reported an error but the value read back is the one written, so the Run continues (`key`, `error_type`) |
| `database_cutover` | INFO | A relocation's cutover pointed `wp-config.php` at the relocated database (`keys`, `wp_config_path`, `from`, `to`; `host_from`, `host_to` when the host changed) |
| `database_drop_warnings` | WARNING | Dropping a database succeeded with server warnings (`db_name`, `warnings`) |
| `database_dropped` | INFO | A database was dropped (`db_host`, `db_port`, `db_name`, `changed`, `warnings`, `duration_ms`) |
| `database_ensured` | INFO | The administrator ensured the load database and its account (`db_name`, `created_database`, `created_user`, `duration_ms`) |
| `database_ensured_for_load` | INFO | The WordPress account could not load the database as it stood, so the administrator provisioned it (`database`, `missing`, `created_database`, `created_user`, `granted`, `warnings`; owner decision 55) |
| `database_load_checked` | INFO | The loaded database was surveyed after the load (`database`, `is_wordpress`, `schema_version`; `performed` `false` in a Dry Run) |
| `database_load_ready` | INFO | The load checks passed at `PREFLIGHT`, as the WordPress account (`database`, `administrator_needed`; `missing` when an administrator will provision) |
| `database_restore_report` | INFO, ERROR | The database part of a restore finished (INFO) or failed (ERROR), with what was done (`database`, `administrator`, `dropped_tables`, loaded, checked, a relocation's cutover keys written) |
| `database_rollback_failed` | ERROR | A failed load's automatic rollback failed, or nothing could roll it back; the Run is recorded as interrupted (`database`, `cause`, `rollback_error`, `snapshot`, `recovery`; owner decision 50) |
| `database_rollback_owed` | WARNING | `--resolve-interrupted-run` acknowledged a Run that left the live database partly loaded; restore it from the snapshot (`run_id_resolved`, `database`, `snapshot`, `recovery`) |
| `database_rollback_started` | WARNING | A load failed after it began; the live database is being put back (`database`, `rollback` `snapshot` or `empty`, `snapshot`, `cause`) |
| `relocation_rollback_started` | WARNING | A URL relocation's content rewrite (`SEARCH_REPLACE`) failed; the database and `wp-config.php` are being put back (`database`, `wp_config_path`, `saved_config`, `cause`; owner decision 72) |
| `relocation_rolled_back` | WARNING | The database and the original `wp-config.php` were put back after a failed `SEARCH_REPLACE` (`database`, `wp_config_path`, `saved_config`, `cause`); the Run then exits with the rewrite's error (owner decision 72) |
| `database_rolled_back` | WARNING | The live database was put back as it was before the Run, from the Safety Snapshot or by emptying it (`database`, `rollback`, `snapshot`, `cause`); the Run then exits with the load's error |
| `database_surveyed` | INFO | A restore target's database was surveyed (`db_name`, `observed_by` `wordpress` or `administrator`, `database_exists`, `prefixed_tables` count, `duration_ms`; owner decision 55) |
| `database_tables_dropped` | INFO | The load dropped every table and view carrying the site's prefix before loading the Dump (`database`, `table_prefix`, `count`, `tables`; owner decision 53) |
| `database_host` | INFO | The host a backup's dump connects to, as `wp-config.php` gives it (`db_host`, `db_port`, `local`); logged, never confirmed (owner decision 54) |
| `deprecated_option` | WARNING | A deprecated option was given: `--admin-tls`/`--admin-tls-ca` (taken as `--db-tls`/`--db-tls-ca`), `--allow-unencrypted` or `--drop-database` (no effect; owner decision 63) (`option`, `replacement`) |
| `dump_loaded` | INFO | A Dump was loaded into the load database, the Archive's or, in a rollback, the Safety Snapshot's (`db_name`, `bytes`, `dropped`, `duration_ms`) |
| `dump_readiness` | INFO | The database reported whether it can be dumped (`db_name`, `credential_source`, blockers and warnings) |
| `encryption_configured` | INFO | A backup encrypts its payload (`scheme` `age`, `mode` `passphrase`) |
| `exclusions_applied` | INFO | The first exclusion decision of a backup (`source`, `pattern_count`) |
| `hard_link_stored_as_file` | WARNING | Files that are hard links of each other were each stored in full (`paths`) |
| `incomplete_run_found` | ERROR | A restore found an interrupted Run's journal for this Site and refuses (`run_id_interrupted`, `started_at`, `interrupted_phase`) |
| `interrupted_run_resolved` | INFO | `--resolve-interrupted-run` completed and cleared the interrupted Run's journal (`run_id_resolved`) |
| `journal_finish_failed` | ERROR | The Run Journal could not be finished after an orderly restore failure; the Run's own error is re-raised (`error`, the class name) |
| `journal_written` | DEBUG | A Run Journal entry was written and fsynced (`journal_run_id`, `phase`, `journal_event`, `step`) |
| `load_database_drop_failed` | ERROR | After a failure, dropping the relocated database this Run created failed; the original error is raised (`database`, `error_type`) |
| `load_database_dropped` | WARNING | After a failure, the relocated database this Run created was dropped (`database`) |
| `manifest_value_unknown` | WARNING | A Manifest field held a value this version does not know; it reads as absent (`field`, `value`) |
| `member_decryption_failed` | ERROR | Decrypting the payload failed (`member`, `reason` `wrong_passphrase` or `corrupt`) |
| `on_warning_failed` | ERROR | A library caller's `on_warning` sink raised on a notice given after the Safety Snapshot was taken; the restore continues (`code`, `error`) |
| `passphrase_below_encryption_floor` | WARNING | A restore's passphrase is shorter than the encryption floor and also encrypts the Safety Snapshot (`minimum`) |
| `passphrase_ignored` | WARNING | `L3IO_WP_BACKUP_PASSPHRASE` is set but encryption was not requested (`variable`) |
| `password_on_command_line` | WARNING | A password was given as a flag value, visible in the process list (`flag`, never the value) |
| `path_excluded` | DEBUG | A backup left a path out by an exclusion pattern, or because it is the Archive being written or an earlier `wpbackup2` Archive (`path`, `pattern`) |
| `phase_completed` | INFO, ERROR | A step finished; see [the record schema](#the-record-schema) |
| `planned_action` | INFO | One action of the ordered plan, before preflight (`ordinal`, `step`, `action`) |
| `post_check_skipped` | WARNING | The search-replace post-check for a URL pair was skipped because the new URL contains the old one (`old`, `new`) |
| `preflight_checked` | DEBUG, INFO, ERROR | One required tool was checked: DEBUG when present, INFO when an optional one is missing, ERROR otherwise (`outcome`) |
| `preflight_passed` | INFO | Every required tool is present (`tools`) |
| `protected_path_kept` | WARNING | A path an exclusion pattern matched was kept because it is protected (`path`, `pattern`) |
| `relocation_planned` | INFO | A restore resolved what it will relocate (`keys`, `cutover_keys`, `db_location`) |
| `restore_mode_resolved` | INFO | The restore flags resolved to one Mode (`restore_mode`, `overrides`, `snapshot_policy`, `flags`) |
| `restore_refused` | ERROR | FR-7 refused a non-empty target (`reason`, `parts`, `found`) |
| `backup_runs_as_site_owner` | INFO | A backup started as root switched to the uid and gid that own the Site Root, after reading the passphrase and the database credentials (`uid`, `gid`, `user`, `home`; owner decision 67) |
| `prune_runs_as_site_owner` | INFO | A prune started as root switched to the uid and gid that own the Site Root (`uid`, `gid`, `user`, `home`; as owner decision 67) |
| `restore_runs_as_site_owner` | INFO | A restore started as root switched to the uid and gid that own the Site Root, after reading what only root may read (`uid`, `gid`, `user`, `home`; owner decision 58) |
| `run_finished` | INFO, ERROR | A Run ended; see [the record schema](#the-record-schema) |
| `run_started` | INFO | A Run began; see [the record schema](#the-record-schema) |
| `safety_snapshot_encrypted` | WARNING | The Safety Snapshot is encrypted with this Run's passphrase, which its restore needs (`path`) |
| `safety_snapshot_kept` | INFO | A failed restore left its Safety Snapshot in place (`path`) |
| `safety_snapshot_not_needed` | INFO | No Safety Snapshot was planned because every part the Mode may destroy is empty (`empty`, `restore_mode`) |
| `safety_snapshot_planned` | INFO | A Dry Run's Safety Snapshot, not written (`path`, `scope`, `restore_mode`) |
| `safety_snapshot_skipped` | WARNING | The Safety Snapshot was opted out of (`opted_out_by`, `restore_mode`) |
| `safety_snapshot_unencrypted` | WARNING | The Safety Snapshot was written unencrypted, as no passphrase resolved (`path`) |
| `safety_snapshot_written` | INFO | The Safety Snapshot was written (`path`, `scope`, `bytes`, `encrypted`, `integrity`) |
| `search_replace_completed` | INFO | WP-CLI search-replace finished (`pairs`, `replacements`, `tables`, `remaining`) |
| `search_replace_count_drift` | WARNING | A pair replaced a different number of values than its preview counted (`old`, `new`, `previewed`, `replaced`) |
| `search_replace_home_moved` | WARNING | One stored value serves `siteurl` and `home`, so relocating one URL moves home-derived links too (`old`, `new`) |
| `secret_too_short_to_redact` | WARNING | A secret shorter than the redaction floor was not registered (`source`, never the value) |
| `secrets_registered` | DEBUG | How many secrets the Run's registry holds (`count`) |
| `site_lock_acquired` | INFO | The Site Lock was taken (`site_root`, `lock_path`) |
| `site_lock_released` | DEBUG | The Site Lock was released (`site_root`, `lock_path`) |
| `site_locked` | ERROR | Another Run holds the Site Lock; the Run exits 9 `LOCKED` (`site_root`, `lock_path`, the holder's `holder_*` fields) |
| `site_root_cleaned` | INFO | A restore cleared the Site Root; a kept entry is matched case-insensitively (`path`, `removed`, `kept`, `duration_ms`) |
| `special_file_skipped` | WARNING | A backup skipped a file that is not regular, a directory or a symlink (`path`) |
| `rollback_failed_not_journaled` | ERROR | A failed rollback could not be journaled; the journal stays at `rollback_started`, still blocking (`error_type`) |
| `stale_lock_reclaimed` | WARNING | A dead Run's lock record was overwritten (`previous_run_id`, `previous_pid`, `previous_started_at`, `previous_pid_alive`) |
| `stale_residue_removed` | INFO | A dead Run's workspace or `.partial` file older than 24 h was removed (`path`) |
| `symlink_outside_site_root` | WARNING | A backup stored a symlink whose target leaves the Site Root (`path`, `target`) |
| `symlink_skipped` | WARNING | A restore did not restore a symlink (`path`, `link_target`, `reason`) |
| `tables_dropped` | INFO | An empty live database was emptied again by a rollback (`db_name`, `dropped`, `duration_ms`) |
| `target_checked` | INFO | A restore surveyed its target before planning (`site_root_empty`, `database_exists`) |
| `target_resurveyed` | INFO | A restore surveyed its target again before the first destructive phase (`site_root_empty`, `restore_mode`) |
| `tracer_failed` | WARNING | The caller's tracer raised; tracing is disabled for the rest of the Run (`tracer`, `error`) |
| `unexpected_exception` | ERROR | A library call failed with a defect, wrapped as `UnexpectedError` (`cause_type`, `api_function`, `traceback`) |
| `unknown_setting_variable` | WARNING | An `L3IO_WP_BACKUP_*` variable is set that no setting has, so nothing reads it (`variable`, a name, never the value; owner decision 60) |
| `what_if_not_applicable` | INFO | `--whatIf` was given to `verify`, which is read-only (`message`) |
| `workspace_created` | DEBUG | The Run workspace was created (`path`) |
| `workspace_not_removed` | WARNING | The Run workspace survived removal; the Run fails (no path) |
| `workspace_not_removed_path` | DEBUG | The path of the workspace that survived removal (`path`) |
| `would_check` | INFO | A Dry Run would survey the loaded database (`database`) |
| `wp_cli_invoked` | DEBUG | A WP-CLI command ran (`argv` redacted, `cwd`, `step`) |
| `wp_cli_running_as_root` | WARNING | WP-CLI runs as root, executing the Site's PHP as root (`site_root`, `archive_encrypted`) |
| `wp_config_exclusion_ignored` | DEBUG | An exclusion pattern matched `wp-config.php`, which is never excluded (`path`) |
| `wp_config_kept` | INFO | A restore kept the target's own `wp-config.php` (`path`) |
| `wp_config_not_written` | INFO | A restore found no `wp-config.php` at the target and wrote none: a database-only restore onto a folder without one (`path`, `reason`; owner decision 74) |
| `wp_config_located` | INFO | A backup found the Site's `wp-config.php` (`path`, `location`) |
| `wp_config_outside_search_path` | WARNING | `--wp-config` names a file WordPress would not load for this Site Root (`path`) |
| `wp_config_overwritten` | WARNING | `--overwrite-wp-config` replaced the target's `wp-config.php`; the old one was saved (`path`, `saved`) |

### The Site Lock

Every backup and restore Run, a Dry Run included, takes the Site Lock before it
reads the Site. The lock is a kernel `flock` on
`<sha256 of the canonical Site Root>.lock` in the per-user state directory,
`platformdirs.user_state_path("l3io-wp-backup") / "locks"` (on Linux
`~/.local/state/l3io-wp-backup/locks`, or under `$XDG_STATE_HOME`), mode `0700`,
beside a `0600` `.json` holder record. The kernel releases the lock when its
holder exits for any reason, SIGKILL included, so a lock is never left held by a
dead Run; the next Run overwrites the dead Run's holder record and logs
`stale_lock_reclaimed`.

A Run refused with exit 9 (`LOCKED`, `site_locked`) found another live Run of
the same Site, by the same OS user, holding the lock; the message names its
`run_id`, PID, host and start time. Nothing was changed. Wait for that Run to
finish and run again; do not delete the lock file, which would not release a
live Run's lock and could let two Runs of one Site overlap.

The per-user state directory (`l3io-wp-backup` and everything the lock, the Run
Journal and WP-CLI's private directory use under it) must be a real directory,
not a symlink, owned by the user running the command and not group- or
world-writable; otherwise the Run is refused before it is created or used (the
lock's refusal is `SiteLockLocationError`, exit 7 `NOT_READY`, naming the
directory and the reason). Fix the directory's owner or mode (`chmod 0700`), or
point `XDG_STATE_HOME` at a directory you own.

"The user running the command" is the effective user, the one that will own
the files the Run creates (owner decision 44). The stale-workspace sweep
judges ownership the same way. Under `sudo -E` the environment, and with it
`XDG_STATE_HOME` (and, unless sudoers resets it, `HOME`), is the invoking
user's while the effective user is root, so the state directory found can be
owned by another user: the refusal names
the directory, both uids and `XDG_STATE_HOME`. Point `XDG_STATE_HOME` at a
directory root owns, or run without `-E`.

## Correlating with your own identifier

A Python caller can bind its own correlation ID before calling the library; it
appears on every record of the Run, beside `l3io_run_id`:

```python
import structlog

structlog.contextvars.bind_contextvars(correlation_id="job-1234")
```

The Run's keys are prefixed `l3io_` (owner decision 2026-11-07, BL-E006-019):
`l3io_run_id` and `l3io_dry_run` are what the Run binds in structlog's context
variables, so a host's own `run_id` or `dry_run` is never overwritten during a
call. No record the package emits has a `run_id` or `dry_run` field of its own,
which would replace a host's bound value in that record: `run_started` states
`l3io_run_id` and `l3io_dry_run`, and `run_finished` `l3io_run_id`, as explicit
fields too, so a host that does not merge context variables still sees each
Run's boundaries named. `tests/unit/cli/test_log_schema.py` checks every record
of a whole backup, real and Dry Run, and every record a Run emits under a host
that bound its own `run_id` and `dry_run`.

## What is and is not covered

- Records emitted before the Run opens (argument translation, the passphrase
  note) carry no `l3io_run_id`. A Run opens after the Site Root is checked.
  `command_failed` carries the `l3io_run_id` of the Run that failed, when one was open.
- A record from a thread started without `contextvars.copy_context().run`
  carries no `l3io_run_id` (contextvars do not cross a raw `threading.Thread`). It
  is still redacted: the redaction step applies every open Run's registry.
- Credentials never appear at any level, `DEBUG` included (FR-20, ADR-0018);
  the workspace path is logged only at `DEBUG` (`workspace_not_removed` is a
  WARNING without it, followed by a `DEBUG` record that names it; a Run that
  had otherwise succeeded then fails with `WorkspaceNotRemovedError`, exit 1).
- A host that configures logging itself gets none of this: the configuration
  lives in `l3io.wp.backup.cli.logging.configure_logging`, which a library
  module never calls (AD-10).
