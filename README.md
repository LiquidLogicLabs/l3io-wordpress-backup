# py-wordpress-backup

A Python package for backing up and restoring a self-hosted WordPress installation, including the database and uploaded content.

The backup file will contain:

 - Your database as a MySQL dump.
 - An exact copy of your entire WordPress directory.

The restore process will:
 - Extract the files into the correct location
 - Update ``wp-config.php`` with new settings (if needed)
 - Create the database if it doesn't exist
 - Add the user and grant it access if it doesn't exist
 - Restore the database

## Credit
Credit for the original version of this goes to [Cariad Eccleston](https://github.com/cariad/py-wordpress-database).  

## Installation

```shell
pip install l3io-wordpress-backup
```

Requirements: Python 3.11 or newer. Extracting an archive also needs a Python with the June 2025 `tarfile` security fixes (3.11.13+, 3.12.11+, 3.13.4+; CVE-2025-4517); on an older one extraction is refused — upgrade Python.

## Usage

To backup:

```
wpbackup2 backup --wp-dir /www/wordpress --archive ~/backup.tar.gz
```

To check an archive without touching a site:

```
wpbackup2 verify --archive ~/backup.tar.gz
```

Without a key, `verify` proves the archive's checksums are **consistent** with its manifest, not that the archive is **authentic**: the manifest is unprotected, so anyone who can rewrite the archive can rewrite its checksums too. It says so (`authenticity not checked (no key)`). For an encrypted archive, `--authenticate` decrypts the whole payload with its passphrase — taken from `L3IO_WP_BACKUP_PASSPHRASE` or a terminal prompt, never from the command line — and `age` authenticates every chunk. A wrong passphrase or a tampered payload exits 8; a plain archive is refused, since there is nothing to authenticate.

`restore` is listed in `--help` but is not available in this build.

`wp-config.php` is read for your database credentials. It is looked for in the WordPress directory, then its parent; pass `--wp-config PATH` to use another.

### Command-line reference

Generated from the parser by `tools/gen_cli_docs.py`; CI fails if it drifts.

<!-- BEGIN GENERATED: cli-usage (tools/gen_cli_docs.py) -->
```text
$ wpbackup2 --help
usage: wpbackup2 [-h] [--version] [--log-level LOG_LEVEL] [--log-format {console,json}] [--whatIf]
                 {backup,restore,verify,doctor} ...

Backup/restore your WordPress content and database.

positional arguments:
  {backup,restore,verify,doctor}
                        Action being requested
    backup              Perform a WordPress backup
    restore             Perform a WordPress restore
    verify              Check an archive without touching a site
    doctor              Report whether this host is ready to back up and restore

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --log-level LOG_LEVEL
                        Logging level (stdlib level names; default INFO)
  --log-format {console,json}
                        Log format on stderr (default: console on a terminal, else json)
  --whatIf, --what-if   Dry Run: resolve credentials, run preflight and every query for real,
                        print the ordered plan, and change nothing

exit codes:
   0  OK                   the Run succeeded
   1  FAILED               the Run failed for a reason no other code names
   2  USAGE                the command line was invalid
   3  REFUSED              a check found a reason not to act; nothing was done
   4  CREDENTIALS          credentials were missing, unusable or rejected
   5  UNREACHABLE          the database server could not be reached
   7  NOT_READY            a required tool or optional extra is not available
   8  VERIFICATION_FAILED  the archive failed verification
   9  LOCKED               another Run holds the site lock
```

```text
$ wpbackup2 backup --help
usage: wpbackup2 backup [-h] --wp-dir WP_DIR [--archive ARCHIVE] [--wp-config PATH]
                        [--mode {files,db,all}] [--encrypt-with-passphrase] [--exclude PATTERN]
                        [--no-default-excludes] [--list-default-excludes]
                        [--db-credentials-aws-secret-id ID] [--db-credentials-aws-region REGION]

options:
  -h, --help            show this help message and exit
  --wp-dir WP_DIR       Path to the root of the WordPress directory
  --archive ARCHIVE     Archive path or file: URL (default: ./wpbackup2-<db>-<UTC
                        timestamp>.tar.gz)
  --wp-config PATH      use this wp-config.php instead of searching for it
  --mode {files,db,all}
                        What to back up: files, db, or all (default: all)
  --encrypt-with-passphrase
                        Encrypt the archive with a passphrase. The passphrase comes from
                        L3IO_WP_BACKUP_PASSPHRASE or a prompt, never the command line
  --exclude PATTERN     Leave matching paths out of the archive (repeatable)
  --no-default-excludes
                        Do not apply the default exclusion set
  --list-default-excludes
                        Print the default exclusion set and exit without backing up

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id)
```

```text
$ wpbackup2 restore --help
usage: wpbackup2 restore [-h] --wp-dir WP_DIR [--archive ARCHIVE] [--mode {files,db,all}]
                         [--cleanfirst] [--drop-database] [--force] [--wp-config PATH]
                         [--overwrite-wp-config] [--no-safety-snapshot] [--snapshot-dir PATH]
                         [--resolve-interrupted-run RUN_ID] [--admin-username ADMIN_USERNAME]
                         [--admin-password ADMIN_PASSWORD]
                         [--admin-credentials-aws-secret-id ADMIN_CREDENTIALS_AWS_SECRET_ID]
                         [--admin-credentials-aws-region ADMIN_CREDENTIALS_AWS_REGION]
                         [--only-appointed-in-asg] [--new-site-url NEW_SITE_URL]
                         [--new-site-home-url NEW_SITE_HOME_URL] [--new-db-host NEW_DB_HOST]
                         [--new-db-name NEW_DB_NAME] [--new-db-user NEW_DB_USER]
                         [--new-db-password NEW_DB_PASSWORD] [--db-credentials-aws-secret-id ID]
                         [--db-credentials-aws-region REGION]

options:
  -h, --help            show this help message and exit
  --wp-dir WP_DIR       Path to the root of the WordPress directory
  --archive ARCHIVE     Path or file: URL of the archive to restore from

restore mode:
  Together these select one Restore Mode; any other combination is refused.

  --mode {files,db,all}
                        What to restore: files, db, or all (default: all)
  --cleanfirst          Clear the Site Root before restoring the files. It does not remove the
                        database: that is --drop-database
  --drop-database       Remove the previous database once the restored one has taken over. --mode
                        all needs --cleanfirst with it
  --force               Restore into a non-empty target for the parts the mode restores. It
                        selects no mode and removes nothing

restore options:
  --wp-config PATH      use this wp-config.php instead of searching for it
  --overwrite-wp-config
                        Replace the target's existing wp-config.php with the archive's (only with
                        a mode that restores files)
  --no-safety-snapshot  Do not take the Safety Snapshot before changing anything
  --snapshot-dir PATH   Directory to write the Safety Snapshot to (default: the per-user state
                        directory). Refused inside the Site Root or the wp-config.php directory; a
                        directory a web server serves from elsewhere is not detected
  --resolve-interrupted-run RUN_ID
                        Resolve the journal left by the interrupted Run with this ID
  --admin-username ADMIN_USERNAME
                        Database administrator username (give --admin-password with it)
  --admin-password ADMIN_PASSWORD
                        Database administrator password. The value is visible in the process list;
                        prefer L3IO_WP_BACKUP_ADMIN_PASSWORD
  --admin-credentials-aws-secret-id ADMIN_CREDENTIALS_AWS_SECRET_ID
                        Read the database administrator from this AWS Secrets Manager secret
  --admin-credentials-aws-region ADMIN_CREDENTIALS_AWS_REGION
                        AWS region of --admin-credentials-aws-secret-id (needs the secret id)
  --only-appointed-in-asg
                        Removed with chesney; accepted only to explain that
  --new-site-url NEW_SITE_URL
                        Relocate the Site to this URL
  --new-site-home-url NEW_SITE_HOME_URL
                        Relocate the Site's home URL
  --new-db-host NEW_DB_HOST
                        Relocate to this database host
  --new-db-name NEW_DB_NAME
                        Relocate to this database name
  --new-db-user NEW_DB_USER
                        Relocate to this database user
  --new-db-password NEW_DB_PASSWORD
                        Password of --new-db-user. The value is visible in the process list

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id)
```

```text
$ wpbackup2 verify --help
usage: wpbackup2 verify [-h] --archive ARCHIVE [--authenticate]

options:
  -h, --help         show this help message and exit
  --archive ARCHIVE  Path or file: URL of the archive to check.
  --authenticate     Decrypt the encrypted payload to prove the archive authentic; without it,
                     verify proves only that its checksums are consistent. The passphrase comes
                     from L3IO_WP_BACKUP_PASSPHRASE or a prompt.
```

```text
$ wpbackup2 doctor --help
usage: wpbackup2 doctor [-h] [--wp-dir DIR] [--archive TARGET] [--json]

options:
  -h, --help        show this help message and exit
  --wp-dir DIR      Also check this Site Root
  --archive TARGET  Also check this archive target
  --json            Print the diagnosis as JSON
```
<!-- END GENERATED: cli-usage -->

### Exit codes

A scheduler can branch on the exit code alone: `0` means the Run succeeded and
every failure exits non-zero with a code naming its kind. The table is
append-only — a code is never renumbered or reused. `KeyboardInterrupt` exits
with the shell's `130`, which is not part of the table.

<!-- BEGIN GENERATED: exit-codes (tools/gen_cli_docs.py) -->
| Code | Name | Meaning |
|---:|---|---|
| 0 | `OK` | the Run succeeded |
| 1 | `FAILED` | the Run failed for a reason no other code names |
| 2 | `USAGE` | the command line was invalid |
| 3 | `REFUSED` | a check found a reason not to act; nothing was done |
| 4 | `CREDENTIALS` | credentials were missing, unusable or rejected |
| 5 | `UNREACHABLE` | the database server could not be reached |
| 7 | `NOT_READY` | a required tool or optional extra is not available |
| 8 | `VERIFICATION_FAILED` | the archive failed verification |
| 9 | `LOCKED` | another Run holds the site lock |

Reserved for the `l3io-wp-*` family and never used: 6.
<!-- END GENERATED: exit-codes -->

## Development

### Prerequisites

Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/).

### Installing dependencies

```shell
uv sync
```
