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

## Usage

To backup:

```
wpbackup2 backup --wp-dir /www/wordpress --archive ~/backup.tar.gz
```

To check an archive without touching a site:

```
wpbackup2 verify --archive ~/backup.tar.gz
```

`restore` is listed in `--help` but is not available in this build.

`wp-config.php` is read for your database credentials. It is looked for in the WordPress directory, then its parent; pass `--wp-config PATH` to use another.

### Command-line reference

Generated from the parser by `tools/gen_cli_docs.py`; CI fails if it drifts.

<!-- BEGIN GENERATED: cli-usage (tools/gen_cli_docs.py) -->
```text
$ wpbackup2 --help
usage: wpbackup2 [-h] [--version] [--log-level LOG_LEVEL] [--log-format {console,json}]
                 {backup,restore,verify} ...

Backup/restore your WordPress content and database.

positional arguments:
  {backup,restore,verify}
                        Action being requested
    backup              Perform a WordPress backup
    restore             Perform a WordPress restore
    verify              Check an archive without touching a site

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --log-level LOG_LEVEL
                        Logging level (stdlib level names; default INFO)
  --log-format {console,json}
                        Log format on stderr (default: console on a terminal, else json)

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
                        [--mode {files,db,all}]

options:
  -h, --help            show this help message and exit
  --wp-dir WP_DIR       Path to the root of the WordPress directory
  --archive ARCHIVE     Archive path or file: URL (default: ./wpbackup2-<db>-<UTC
                        timestamp>.tar.gz)
  --wp-config PATH      use this wp-config.php instead of searching for it
  --mode {files,db,all}
                        What to back up: files, db, or all (default: all)
```

```text
$ wpbackup2 restore --help
usage: wpbackup2 restore [-h]

options:
  -h, --help  show this help message and exit
```

```text
$ wpbackup2 verify --help
usage: wpbackup2 verify [-h] --archive ARCHIVE

options:
  -h, --help         show this help message and exit
  --archive ARCHIVE  Path or file: URL of the archive to check.
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
