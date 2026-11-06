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

### External tools

A Run checks every external tool its plan needs before its first effect, and stops with exit 7 (`NOT_READY`) naming the tool, the step that needed it and what was found. The table lists the tools this package runs itself and the minimum a plan in this build asks for:

<!-- BEGIN GENERATED: tool-requirements (tools/gen_tool_docs.py) -->
| Tool | Minimum | Purpose | Needed for | Install |
|---|---|---|---|---|
| `age` | — | encrypts the Archive under a passphrase (CLI passphrase backend) | no plan in this build | https://github.com/FiloSottile/age#installation |
| `age-plugin-batchpass` | — | supplies the passphrase to age non-interactively (CLI backend) | no plan in this build | https://github.com/FiloSottile/age/releases |
| `wp` | — | WP-CLI, which relocation runs to rewrite the Site's URLs | no plan in this build | https://wp-cli.org/#installing |
| MySQL/MariaDB client tools | client 8.4+, MariaDB 11.4+ | the database dump and load | checked per Site by l3io-wordpress-database (`check_dump`/`check_load`), never run by this package | your distribution's MySQL or MariaDB client package |
<!-- END GENERATED: tool-requirements (tools/gen_tool_docs.py) -->

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

To restore (see [docs/restore.md](docs/restore.md) for every mode, the refusals and their exit codes):

```
wpbackup2 restore --wp-dir /www/wordpress --archive ~/backup.tar.gz --mode all
```

A restore takes a Safety Snapshot of whatever it would change first, and refuses a non-empty target unless a flag authorizes it (exit 3, naming the flag). A target with no `wp-config.php` takes its database from the archive's config only when you confirm it with `--archive-database USER@HOST[:PORT]/NAME`.

`wp-config.php` is read for your database credentials. It is looked for in the WordPress directory, then its parent; pass `--wp-config PATH` to use another.

### Credentials

No credential has to be on the command line, so a cron line needs none. Each account's credentials come from the first source that has them, in a fixed order:

| Account | 1. Explicit | 2. Environment | 3. `wp-config.php` |
|---|---|---|---|
| WordPress database (backup) | library `BackupRequest.credentials` | `L3IO_WP_BACKUP_DB_USER` and `L3IO_WP_BACKUP_DB_PASSWORD` | `DB_USER`/`DB_PASSWORD` |
| WordPress database (restore) | none | none | `DB_USER`/`DB_PASSWORD`, the account the restored site will use |
| Administrator (restore) | `--admin-username`/`--admin-password`, or library `RestoreRequest.admin` | `L3IO_WP_BACKUP_ADMIN_USER` and `L3IO_WP_BACKUP_ADMIN_PASSWORD` | never |

- **Both variables of a pair, or neither.** Setting one without its partner is an error (exit 4), not a fall-through to `wp-config.php`. A variable set to an empty value counts as set. The one exception is `--admin-username` without `--admin-password`: the password then comes from `L3IO_WP_BACKUP_ADMIN_PASSWORD`, and an empty value there counts as unset.
- **A source that fails stops the search.** An unreadable AWS secret, for example, never becomes the next source's account. The administrator is never taken from `wp-config.php` or from the WordPress account. A restore that needs one and has none is refused (exit 4).
- **AWS Secrets Manager.** A secret holding the JSON `{"username": ..., "password": ...}` can supply either account as its explicit source. Install the `aws` extra: `pip install 'l3io-wordpress-backup[aws]'`. Without it, exit 7 names the extra. Without a region from the request or the AWS session, exit 4 names the region flag. In this release only the library selects it: `AwsSecretCredentialSpec(secret_id, region)` as `BackupRequest.credentials` or `RestoreRequest.admin`. The `--db-credentials-aws-*` and `--admin-credentials-aws-*` flags are accepted but not implemented yet.
- **The log names the source, never the value.** Each resolution logs `credential_source_selected` with the source's label, for example `environment:L3IO_WP_BACKUP_DB_USER,L3IO_WP_BACKUP_DB_PASSWORD`.

The environment of a process can be read by the same user and by root (`/proc/<pid>/environ`), and `--admin-password` is visible to every user in `ps`. On a shared host, prefer `wp-config.php` or AWS Secrets Manager.

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
    doctor              Report whether this host is ready to back up and restore (not available in
                        this build)

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
                        L3IO_WP_BACKUP_PASSPHRASE or a prompt, never the command line (not
                        available in this build)
  --exclude PATTERN     Leave matching paths out of the archive (repeatable) (not available in
                        this build)
  --no-default-excludes
                        Do not apply the default exclusion set (not available in this build)
  --list-default-excludes
                        Print the default exclusion set and exit without backing up (not available
                        in this build)

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
                        (not available in this build)
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id) (not
                        available in this build)
```

```text
$ wpbackup2 restore --help
usage: wpbackup2 restore [-h] --wp-dir WP_DIR [--archive ARCHIVE] [--mode {files,db,all}]
                         [--cleanfirst] [--drop-database] [--force] [--whatIf] [--wp-config PATH]
                         [--overwrite-wp-config] [--reset-existing-db-user] [--no-safety-snapshot]
                         [--snapshot-dir PATH] [--archive-database USER@HOST[:PORT]/NAME]
                         [--admin-tls {required,verify-ca,verify-identity}] [--admin-tls-ca FILE]
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
  --whatIf, --what-if   Dry Run: run every check and query for real, print the ordered plan, and
                        change nothing (also accepted before `restore`)
  --wp-config PATH      use this wp-config.php instead of searching for it (not available in this
                        build)
  --overwrite-wp-config
                        Replace the target's existing wp-config.php with the archive's (only with
                        a mode that restores files)
  --reset-existing-db-user
                        When the target has no wp-config.php and the archive's database account
                        already exists: reset its password to the archive's (refused otherwise)
  --no-safety-snapshot  Do not take the Safety Snapshot before changing anything
  --snapshot-dir PATH   Directory to write the Safety Snapshot to (default: the per-user state
                        directory). Refused inside the Site Root or the wp-config.php directory; a
                        directory a web server serves from elsewhere is not detected
  --archive-database USER@HOST[:PORT]/NAME
                        When the target has no wp-config.php: confirm the database the archive's
                        config names (user, host, port and name must all match), or the restore is
                        refused
  --admin-tls {required,verify-ca,verify-identity}
                        Raise the TLS the database connections use (default: verified TLS to any
                        host but localhost, 127.0.0.1 and ::1; never lowered)
  --admin-tls-ca FILE   CA bundle that verifies the database server (default: the system store)
  --resolve-interrupted-run RUN_ID
                        Resolve the journal left by the interrupted Run with this ID
  --admin-username ADMIN_USERNAME
                        Database administrator username (give --admin-password with it)
  --admin-password ADMIN_PASSWORD
                        Database administrator password. The value is visible in the process list;
                        prefer L3IO_WP_BACKUP_ADMIN_PASSWORD
  --admin-credentials-aws-secret-id ADMIN_CREDENTIALS_AWS_SECRET_ID
                        Read the database administrator from this AWS Secrets Manager secret (not
                        available in this build)
  --admin-credentials-aws-region ADMIN_CREDENTIALS_AWS_REGION
                        AWS region of --admin-credentials-aws-secret-id (needs the secret id) (not
                        available in this build)
  --only-appointed-in-asg
                        Removed; accepted only to say so (not available in this build)
  --new-site-url NEW_SITE_URL
                        Relocate the Site to this URL (not available in this build)
  --new-site-home-url NEW_SITE_HOME_URL
                        Relocate the Site's home URL (not available in this build)
  --new-db-host NEW_DB_HOST
                        Relocate to this database host (not available in this build)
  --new-db-name NEW_DB_NAME
                        Relocate to this database name (not available in this build)
  --new-db-user NEW_DB_USER
                        Relocate to this database user (not available in this build)
  --new-db-password NEW_DB_PASSWORD
                        Password of --new-db-user. The value is visible in the process list (not
                        available in this build)

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
                        (not available in this build)
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id) (not
                        available in this build)
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
