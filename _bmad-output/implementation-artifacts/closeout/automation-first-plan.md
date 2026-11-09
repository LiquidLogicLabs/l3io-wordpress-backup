# Automation-first change set (owner decisions 49 to 66, 2026-11-08)

**Goal.** `wpbackup2 backup` and `wpbackup2 restore --archive latest` should run unattended with no extra flags. That covers a Docker sidecar next to the official `wordpress` image, and a simple pip install run from cron. Safety comes from defaults, logging, the Dry Run, and the Safety Snapshot with automatic rollback, not from confirmation flags.

The decisions are recorded in [owner-decisions.md](owner-decisions.md). The ADRs record what the owner already decided, so they are marked **Accepted**, citing those decisions. The exception is the config-library choice in step 1, an engineering choice that is **Proposed** until the owner reviews it.

## Work, in dependency order

1. **Configuration layer** (decision 60).
   - Every setting gets a CLI flag, an `L3IO_WP_BACKUP_*` variable and a config-file key.
   - Precedence: CLI, then environment, then config file, then defaults.
   - A config file that holds a secret must not be group- or world-readable.
   - The library is chosen by a new ADR that evaluates maintained libraries (for example ConfigArgParse or pydantic-settings); no hand-rolled layering.
   - A contract test derives the flag, variable and key mapping from one source.
   - New settings: `--wp-dir` / `WP_DIR`, `--archive-dir` / `ARCHIVE_DIR`, `REQUIRE_ENCRYPTED`, `--db-tls*`, and CLI equivalents for any variable that lacks one.
2. **Backup**.
   - Decision 54: drop `--confirm-db-host`; log the host and show it in the Dry Run.
   - Decision 60: the default target is never inside the Site Root (the current directory if it is outside the site, else a per-user data directory); an explicit in-site target proceeds with a WARNING; drop `--allow-target-in-site-root`.
   - Decision 61: a database password shorter than the redaction floor is accepted with WARNING `credential_below_redaction_floor`.
3. **Database TLS** (decision 56). Connections use no TLS requirement by default, the way WordPress connects. `--db-tls` and `--db-tls-ca` are opt-in and replace the `--admin-tls*` policy. Amend the BL-E002-114 policy and its ADR text.
4. **Restore: direct load** (decisions 49, 50, 53, 55; ADR-0022 amended, ADR-0027 rewritten and Accepted).
   - Order: preflight with the WordPress account, then the Safety Snapshot, then drop every table carrying the site's prefix, then load the Dump.
   - On any failure after loading starts, reload the snapshot automatically. If that rollback fails, record the Run as interrupted, and name both failures, the snapshot and the recovery command.
   - Remove the staging database `<name>_l3s_<id>` and the `DB_NAME` cutover in `wp-config.php`.
   - An administrator is needed only when the database does not exist or the account lacks the rights. Then the Run refuses early and says what is missing.
   - This works for literal and environment-driven `DB_*` alike, and removes `EnvironmentDrivenTargetError` for database Modes.
   - The journal, undo and `--resolve-interrupted-run` are adapted to the new phases.
   - Item B: `--drop-database` is removed, or kept as a no-op that warns.
   - Check database-library support: is there a "drop prefixed tables" operation and a load into an existing database? Request any missing API from the database agent, never issue SQL from backup (AD-28).
5. **Restore guards**.
   - Decision 57: `--force` only when the target does not look like WordPress (files: no `wp-config.php`, `wp-includes/` or `wp-content/`; database: tables but none with the site's prefix).
   - Decision 58: started as root, read what only root can read, then drop to the Site Root owner's uid and gid; remove `--allow-root`; refuse a root-owned Site Root.
   - Decision 59: a passphrase with an unencrypted Archive gives WARNING `archive_not_encrypted` and proceeds; `--require-encrypted` is the hard stop; `--allow-unencrypted` is removed or kept as a no-op.
   - Decision 65: an encrypted Archive's database settings are trusted on a bare server; an unencrypted one still needs `--archive-database`.
   - Decision 64: `--archive latest` picks the newest Archive for this site by the Manifest's `created_at` in the Archive directory, logged and shown in the Dry Run.
6. **Sidecar image defaults.** Set `L3IO_WP_BACKUP_WP_DIR=/var/www/html` and `L3IO_WP_BACKUP_ARCHIVE_DIR=/backups`. `tests/sidecar/run.sh` then runs backup and `restore --archive latest` with no flags, on both engines. Every earlier probe becomes an asserted step.
7. **Docs and CHANGELOG.** Cover every removed or changed flag (pre-1.0 behaviour changes), README quick start for the sidecar, docs/restore.md, docs/api.md and docs/logging.md.

## Done means
- Every gate passes.
- The sidecar test passes on MySQL 8.0 and MariaDB 11.4.
- `make preflight` is green on the full local matrix: Python 3.11 to 3.14 against MySQL 8.0, MySQL 8.4 and MariaDB 11.4, which needs database 0.3.5.
- Every GitHub run is green after the push.

## Kept as is
- The 256 MiB Manifest cap, with streaming after the release (decision 66).
- Files keep the `--cleanfirst` choice (decision 62).
- The 12-character encryption floor (decision 61).
