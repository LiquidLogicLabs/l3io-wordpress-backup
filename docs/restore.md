# Restore

How `wpbackup2 restore` puts an Archive back onto a Site, and what it refuses.
The command line itself (`--mode`, `--cleanfirst`, `--force`, `--whatIf`) is
documented by `wpbackup2 restore --help`.

## Restore Modes

The Mode is resolved once from the flags and governs the whole Run. A Mode that
does not name file removal removes no files; one that does not name database
removal drops no database.

| Mode             | Clears the Site Root | Writes files | Loads the Dump | Drops the losing database |
|------------------|----------------------|--------------|----------------|---------------------------|
| `files`          | no                   | yes          | no             | no                        |
| `files-clean`    | yes                  | yes          | no             | no                        |
| `database`       | no                   | no           | yes            | no                        |
| `database-clean` | no                   | no           | yes            | yes                       |
| `all`            | no                   | yes          | yes            | no                        |
| `all-clean`      | yes                  | yes          | yes            | yes                       |

The phases always run in this order, skipping the ones a Mode does not need:
preflight, verify, safety snapshot, clean, extract, rewrite config, load the
Dump, search-replace.

A Mode that loads the database refuses (exit `REFUSED`) when the Archive holds
no Dump, naming the Archive and its backup scope.

## Before anything is changed

Every check below runs before the first destructive step, so a refusal leaves
the Site, the database and every Storage Target as they were.

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

## `wp-config.php`

By default the target's `wp-config.php` is **kept**: the Archive's copy is
never extracted over it, `--cleanfirst` does not remove it, and only the
database cutover changes it. The Run logs `wp_config_kept`. When the target has
no `wp-config.php`, the Archive's copy arrives with the files.

`--overwrite-wp-config` (file Modes only) replaces the target's file with the
Archive's, keeping the target's own `DB_HOST`, `DB_USER`, `DB_PASSWORD` and
`DB_NAME`, so the Site never points at the source's database. A copy of the
original is saved first (mode `0600`) beside the Run Journal, and the Run logs
the WARNING `wp_config_overwritten`. An Archive without a `wp-config.php` is
refused with this flag.

## Symlinks

A symlink whose target is absolute or resolves outside the Site Root is **not
supported**. It is skipped on restore with a WARNING (`symlink_skipped`), and
the Run still exits `OK`. A Dry Run lists those links after its actions; for a
v0 Archive, which records no link targets, they are reported at extraction.

## Dry Run

`--whatIf` prints the ordered actions the real Run would take and runs every
check the real Run runs, so it refuses where the real Run would, with the same
exit code. It changes nothing: not the Site, the database, any Storage Target,
the snapshot directory or the journal directory. Only the Run workspace is
written (an encrypted payload is decrypted there to check the passphrase).
