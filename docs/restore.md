# Restore

How `wpbackup2 restore` puts an Archive back onto a Site, and what it refuses.
Every flag, `--whatIf` included, is listed by `wpbackup2 restore --help`.
`--whatIf` (or `--what-if`) may be given before or after `restore`:
`wpbackup2 --whatIf restore ...` and `wpbackup2 restore --whatIf ...` are the
same Dry Run.

## Restore Modes

The Mode is resolved once from the flags and governs the whole Run. A Mode that
does not name file removal removes no files; one that does not name database
removal drops no database.

| Mode             | Flags                                    | Clears the Site Root | Writes files | Loads the Dump | Drops the losing database |
|------------------|------------------------------------------|----------------------|--------------|----------------|---------------------------|
| `files`          | `--mode files`                           | no                   | yes          | no             | no                        |
| `files-clean`    | `--mode files --cleanfirst`              | yes                  | yes          | no             | no                        |
| `database`       | `--mode db`                              | no                   | no           | yes            | no                        |
| `database-clean` | `--mode db --drop-database`              | no                   | no           | yes            | yes                       |
| `all`            | `--mode all`                             | no                   | yes          | yes            | no                        |
| `all-clean`      | `--mode all --cleanfirst --drop-database` | yes                  | yes          | yes            | yes                       |

`--mode all` is the default, so `--cleanfirst --drop-database` alone selects
`all-clean`. The Mode names are what the log (`restore_mode`), refusals
("Restore Mode files") and the completion line use; the flags are what you
type. Note the one spelling difference: the flag value `db` is the Mode
`database`. Adding `--force` to any row keeps its Mode and only waives the
non-empty-target refusal for the parts that Mode restores (see below). Any
other combination (for example `--mode db --cleanfirst`) exits 2 `USAGE` and
names the flags that would work.

The phases always run in this order, skipping the ones a Mode does not need:
preflight, verify, safety snapshot, clean, extract, rewrite config, load the
Dump, search-replace.

A Mode that loads the database refuses (exit 3 `REFUSED`) when the Archive
holds no Dump, naming the Archive and its backup scope; `--mode files` restores
its files.

## The database after a restore: a new name, by design

A restore never loads the Dump over the live database. It loads it into a new
**staging database** on the same server, named
`<live name, first 51 characters>_l3s_<first 8 characters of the Run ID>` (for
example `example_wp_l3s_90e53fd8`), checks that it is a WordPress database, and
only then switches `wp-config.php`'s `DB_NAME` to it (the cutover). This is
what keeps the live site intact if the load or the check fails (ADR-0022).

So after a successful `database`, `database-clean`, `all` or `all-clean`
restore **the site permanently runs on the staging database's name**; nothing
renames it back. The report says so on its first line:

```
database: the site now runs on 'example_wp_l3s_90e53fd8' (was 'example_wp')
```

The previous database is kept by `database` and `all`, and dropped (after its
WordPress user's grants on it are revoked) by `database-clean` and
`all-clean`. Either way, **update anything outside this tool that names the
old database**: cron jobs or scripts that dump it, monitoring, other
applications' grants, and your own backup jobs if they name the database
rather than reading `wp-config.php`. A later restore of the same Site names
its staging database after the database the site runs on at that time, so a
second restore gives a name like `example_wp_l3s_90e53fd8_l3s_1a2b3c4d`; the
live part is cut to 51 characters, so a name never exceeds MySQL's 64.

## Exit codes

| Exit | Name                  | A restore exits with it when                                                                 |
|------|-----------------------|----------------------------------------------------------------------------------------------|
| 0    | `OK`                  | it completed (a Dry Run: its plan completed without a refusal)                              |
| 1    | `FAILED`              | something failed, including a database load the report does not call restored               |
| 2    | `USAGE`               | the flags express no Restore Mode, `--archive` is missing, or `--overwrite-wp-config` is given with a Mode that restores no files |
| 3    | `REFUSED`             | a check refused: a non-empty target, an incomplete earlier Run, an unreadable Site Root, an unconfirmed Archive database, a missing Dump, an encrypted Archive with no passphrase, a snapshot that could not be made |
| 4    | `CREDENTIALS`         | a Mode that touches the database has no administrator credentials                           |
| 5    | `UNREACHABLE`         | the database server cannot be reached                                                       |
| 8    | `VERIFICATION_FAILED` | the Archive is malformed or corrupt, or a passphrase is wrong                                |
| 9    | `LOCKED`              | another Run holds this Site's lock                                                          |

## Non-empty targets

A restore never overwrites something it was not told it may. Before any phase
is planned, each part the Mode restores is checked:

- **files** — the Site Root holds any entry;
- **database** — the live database (or the `--new-db-name` database) exists
  with at least one table or view. The live database is always surveyed by the
  administrator in a Mode that restores the database; "not looked at" is never
  taken as empty.

A non-empty part is refused (exit 3 `REFUSED`) unless a flag waives it. The
message names the Mode as you typed it, every non-empty part with what was
found, and the exact flags that would authorize it, for example:

```
refusing to restore with --mode files (Restore Mode files) over a non-empty
target (files): found '/srv/www' (3 entries); nothing was changed.
--mode files --force would authorize it
```

`--force` waives the parts the Mode restores; `--cleanfirst` and
`--drop-database` choose the clean Modes. Waiving the files never waives the
database. There is no prompt. A Dry Run refuses the same way, with the same
exit code. The log record is `restore_refused` (`reason`, `parts`, `found`,
`detail`, `authorizing`, `restore_mode`); when the guard passes, the Run logs
`target_checked` with what it found and the waivers applied.

A Site Root that exists but cannot be read is refused (exit 3), never read as
empty; a missing one fails (exit 1).

## A target with no `wp-config.php`

The Site Descriptor then comes from the Archive's config member, read into the
Run workspace (mode `0600`). Because an Archive can be forged, the database it
names is used only when you confirm it: `--archive-database
USER@HOST[:PORT]/NAME` must name the same user, host (case-insensitive), port
and database as the Archive's config, or the restore is refused (exit 3) before
any database connection. Name the database from your own records, not from
the Archive. When the flag is absent, the refusal deliberately does not show
the Archive's database, so it cannot be pasted back without checking it
(RT-S02-003). When yours differs, the refusal shows the Archive's user, host,
port and database, never a password, and names which of them differ; treat a
difference you did not expect as a sign the Archive may not be what it claims.
The refusal never echoes what you typed. A user part holding `:` (as in `USER:PASSWORD@…`)
is refused as a usage error (exit 2) without being shown. With a target `wp-config.php` the flag is not
needed and is ignored (logged `archive_database_unused`).

## TLS to the database

A restore's database connections use verified TLS (`verify-identity`, against
the system CA store) to any host but `localhost`, `127.0.0.1` and `::1`, which
stay plaintext. `--admin-tls {required,verify-ca,verify-identity}` may raise
that, never lower it: a value below the host's policy exits 2 `USAGE` before
any connection. `--admin-tls-ca FILE` names the CA bundle for a private CA. A
server that cannot complete the required handshake exits 5 `UNREACHABLE`; one
whose certificate does not verify exits 3 `REFUSED`. Backups are unchanged.

## Interrupted Runs

A Run killed part-way leaves its journal open. The next restore of the Site is
refused (exit 3), naming the interrupted Run, its phase, how to undo it and the
journal's path. `--resolve-interrupted-run RUN_ID` acknowledges it (completing
an interrupted `wp-config.php` cutover first) and stops: resolving and
restoring are never one command. A different ID is refused, naming the
journal's.

Before completing a cutover, resolution surveys the database the interrupted
Run was switching to. If it no longer exists or is not a WordPress database,
nothing is written and the resolution is refused (exit 3 `REFUSED`), naming
it: set the keys already written back to the values the journal records, or
restore from the Safety Snapshot, then resolve again. When `wp-config.php`
already holds its pre-Run values (no key landed, or you set them back), there
is nothing to complete: resolution writes nothing, logs the WARNING
`cutover_abandoned`, and acknowledges the Run.

## Before anything is changed

Every check below runs before the first destructive step, so a refusal leaves
the Site, the database and every Storage Target as they were.

The Archive is first copied into the Run workspace (mode `0600`), and every
later read — verification, extraction, the config member — reads that copy, so
a file replaced at the Storage Target after verification is never restored.

**Trust.** Verification of an unencrypted Archive proves its checksums are
consistent with its Manifest, not that it is authentic: whoever can write the
Archive can rewrite both. The restore proceeds: before it changes anything it
logs the WARNING `archive_unauthenticated` and writes a warning to stderr,
which names an Archive with no checksums at all (a v0 Archive,
`STRUCTURAL_ONLY`) as such. Restore only Archives from a source
you trust, or encrypted ones, whose payload `age` authenticates.

1. **Preflight** — the required tools, the passphrase backend when the Archive
   is encrypted, the interpreter's tarfile fixes, free space, and (for a Mode
   that loads the Dump) the database checks.
2. **Verify** — the Archive is verified. A corrupt Archive exits
   `VERIFICATION_FAILED` (8). An encrypted Archive's payload is then decrypted
   into the Run workspace; a wrong passphrase or a damaged payload also exits
   `VERIFICATION_FAILED`. A v0 Archive (no Manifest) is reported as
   `STRUCTURAL_ONLY`, never as `VERIFIED`. An Archive that changed between
   planning and verification is refused with the same exit code.

### Interpreter

Extraction relies on Python's `tarfile` `data` filter, which could be escaped
before the June 2025 security fixes (CVE-2025-4517, CVE-2025-4330,
CVE-2025-4138, CVE-2024-12718). On a Python without them (before 3.11.13,
3.12.11 or 3.13.4) every restore is refused in preflight, before `files-clean`
or `all-clean` deletes anything. Upgrade Python to a patched release.

### Size and free-space limits

Two limits, each changed only by a recorded decision:

- **Free space margin: 5%.** The files a restore writes onto the target (the
  Site Root files, and a laid-down `wp-config.php`) must fit in the target
  filesystem's free space less 5%. What the Run workspace holds — the decrypted
  payload, the Dump and the copy of the Archive's `wp-config.php` — must fit in
  the workspace filesystem's free space less 5%. The Dump never lands on the
  target.
- **Expansion ratio: 200.** An Archive whose members expand to more than 200
  times its compressed size is refused as a decompression bomb.

Both refusals exit `REFUSED`, naming the filesystem that is short, the bytes
needed and the bytes free. For a v1 Archive the sizes come from its Manifest;
for a v0 Archive they are read from its tar headers. During extraction a member
whose header states more bytes than its Manifest entry is refused.

When the Run takes a Safety Snapshot of the files, the Site Root's current file
bytes are charged to the snapshot directory's filesystem. Destinations on the
same filesystem (the target, the workspace and the snapshot directory) are
summed against its one free space. The database part of a snapshot is not
sized in advance: its Dump size is not known before the dump.

## Safety Snapshot

Before the first destructive phase the Run writes a Safety Snapshot: an
Archive of every part the Mode may destroy that is not empty (`files`,
`database` or both), written by the backup path inside the restore's own Run
(one Run ID, one Site Lock). It is verified again after it is published; a
snapshot that cannot be written and verified stops the restore with
`REFUSED` before anything is destroyed, and a written-but-unverified file is
left in place. Its log record is `safety_snapshot_written`, with its path.

`--no-safety-snapshot` skips it and logs the WARNING `safety_snapshot_skipped`.
When every part the Mode touches is empty, no snapshot is taken and the Run
logs `safety_snapshot_not_needed`. `--snapshot-dir` is refused inside the Site
Root or the `wp-config.php` directory; a directory a web server serves from
elsewhere is not detected. In a Dry Run nothing is written and the snapshot is
reported as "would verify".

Immediately before the first destructive phase the target is surveyed again.
If a part that was empty at planning now holds content the snapshot does not
cover, or the non-empty guard now refuses, the restore stops with `REFUSED`
before anything is destroyed. That Run's journal stays open (nothing was
changed), so the next restore names it until it is resolved.

Without a passphrase the snapshot is written unencrypted and the Run says so on
stderr (`safety snapshot is NOT encrypted: <path>`). When a later phase fails,
the snapshot's path is printed with the failure.

## `wp-config.php`

By default the target's `wp-config.php` is **kept**: the Archive's copy is
never extracted over it, `--cleanfirst` does not remove it, and only the
database cutover changes it. The Run logs `wp_config_kept`. When the target has
no `wp-config.php`, the Archive's copy arrives with the files.

`--overwrite-wp-config` (file Modes only; with `--mode db` it exits 2
`USAGE`) replaces the target's file with the
Archive's, keeping the target's own `DB_HOST`, `DB_USER`, `DB_PASSWORD` and
`DB_NAME`, so the Site never points at the source's database. A copy of the
original is saved first (mode `0600`) beside the Run Journal, and the Run logs
the WARNING `wp_config_overwritten`. An Archive without a `wp-config.php` is
refused with this flag (exit 3; omit it to keep the target's), as is one whose
`wp-config.php` is larger than 1 MiB, before anything is changed.

## Symlinks

A symlink whose target is absolute or resolves outside the Site Root is **not
supported**. It is skipped on restore with a WARNING (`symlink_skipped`), and
the Run still exits `OK`. A Dry Run lists those links after its actions; for a
v0 Archive, which records no link targets, they are reported at extraction.

## Dry Run

`wpbackup2 restore --whatIf ...` (or `wpbackup2 --whatIf restore ...`) prints
the ordered actions the real Run would take and runs every check the real Run
runs, so it refuses where the real Run would, with the same exit code. It
changes nothing: not the Site, the database, any Storage Target, the snapshot
directory or the journal directory. Only the Run workspace is written (an
encrypted payload is decrypted there to check the passphrase).

## What a Run prints

Results go to stdout; warnings and the log go to stderr.

With the default (console) log format, stdout carries the Dry Run's ordered
actions, the database report, the search-replace count, the Safety Snapshot's
path and whether `wp-config.php` was kept, each when there is one, and ends
with one completion line naming the Mode, the Archive and the Site:
`restore complete (...)`, or for a Dry Run `Dry Run complete (...); nothing was
changed`. A Run that fails prints no completion line; if it had already taken
a Safety Snapshot it prints `safety snapshot: <path>`.

With `--log-format json`, stdout is exactly one JSON object and nothing else:

```
{"restore": {"status": "completed", "run_id": "...", "dry_run": false,
 "restore_mode": "files", "archive": "...", "site_root": "...",
 "resolved_run_id": null, "actions": [], "skipped_links": [],
 "database_report": null, "search_replace": null, "snapshot_path": null,
 "snapshot_encrypted": false, "wp_config_kept": true}}
```

(shown wrapped; it is one line). `status` is `completed`, `planned` (a Dry
Run, whose `actions` list the plan), `resolved` or `would_resolve`
(`--resolve-interrupted-run`), or `failed` (the database report does not call
the database restored). `database_report` holds the same fields as the
`database_restore_report` log record. A Run that fails after its Safety
Snapshot prints `{"restore": {"status": "failed", "error": "<class>",
"snapshot_path": "<path>"}}`.

## Worked examples

Real output of a files-only Site, with the log on stderr at WARNING
(`--log-level WARNING`). The temporary directory prefix is replaced by
`/srv/www/example` and `/backups`, the state directory by `<state>`, and long
log records are cut.

A refusal: the Site Root is not empty and `--force` was not given.

```
$ wpbackup2 restore --wp-dir /srv/www/example --archive /backups/example.tar.gz --mode files
WARNING: the archive is not encrypted, so it cannot be authenticated; restore only an archive from a source you trust
wpbackup2: error: refusing to restore with --mode files (Restore Mode files) over a non-empty target (files): found '/srv/www/example' (4 entries); nothing was changed. --mode files --force would authorize it [exit 3 REFUSED: a check found a reason not to act; nothing was done]
$ echo $?
3
```

A Dry Run of the authorized command:

```
$ wpbackup2 restore --whatIf --wp-dir /srv/www/example --archive /backups/example.tar.gz --mode files --force
WARNING: the archive is not encrypted, so it cannot be authenticated; restore only an archive from a source you trust
preflight: required tools, the interpreter's tarfile fixes, free space
verify /backups/example.tar.gz
safety snapshot of files to <state>/l3io-wp-backup/snapshots/3174eaae...
extract the Archive's files into /srv/www/example
safety snapshot: <state>/l3io-wp-backup/snapshots/3174eaae.../20261106T093634Z-89abe821-....tar.gz
wp-config.php kept (target's); pass --overwrite-wp-config to replace it
Dry Run complete (Restore Mode files, archive /backups/example.tar.gz, site /srv/www/example); nothing was changed
```

The real Run:

```
$ wpbackup2 restore --wp-dir /srv/www/example --archive /backups/example.tar.gz --mode files --force
WARNING: the archive is not encrypted, so it cannot be authenticated; restore only an archive from a source you trust
safety snapshot is NOT encrypted: <state>/l3io-wp-backup/snapshots/3174eaae.../20261106T093634Z-90e53fd8-....tar.gz
safety snapshot: <state>/l3io-wp-backup/snapshots/3174eaae.../20261106T093634Z-90e53fd8-....tar.gz
wp-config.php kept (target's); pass --overwrite-wp-config to replace it
restore complete (Restore Mode files, archive /backups/example.tar.gz, site /srv/www/example)
```

A Mode that loads the database adds the database report before the Safety
Snapshot line: the database the site now runs on and the one it ran on, what
was created, the bytes loaded, the previous database's fate, and finally
`database: restored`.
