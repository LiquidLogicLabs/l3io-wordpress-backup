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

That is everything a backup and a restore need: `l3io-wordpress-database`, `l3io-wordpress-config` and the `l3io-age-passphrase` binder are required dependencies, and every dependency installs from PyPI; no extra index is needed. The optional extras add:

- `aws` — AWS Secrets Manager credentials (boto3): `pip install 'l3io-wordpress-backup[aws]'`;
- `otel` — the OpenTelemetry SDK and a JSON-file exporter, for tracing.

`pip install 'l3io-wordpress-backup[database]'`, the install earlier releases documented, still works: `database` is now an empty extra that adds nothing. A feature used without its extra (`aws`) stops with exit 7 (`NOT_READY`) naming the extra to install. An installation missing a required dependency (installed with `--no-deps`, or pruned afterwards) stops with exit 7 too, saying the installation is incomplete; reinstall `l3io-wordpress-backup`.

Requirements: Python 3.11 or newer. Extracting an archive also needs a Python with the June 2025 `tarfile` security fixes (3.11.13+, 3.12.11+, 3.13.4+; CVE-2025-4517); on an older one extraction is refused — upgrade Python.

Upgrading from 0.2.21: that release can no longer be installed from PyPI, because two of its dependencies (`wpconfigr`, `chesney`) were removed from there. You do not need it: this package reads 0.2.21 Archives itself (`wpbackup2 verify --archive <file>` reports them as `STRUCTURAL_ONLY`, and `restore` restores them). The restore side is tested with Archives in 0.2.21's layout built by the test suite and by extracting the committed 0.2.21-layout fixtures; no test restores an Archive written by a 0.2.21 install.

### External tools

A Run checks every external tool its plan needs before its first effect, and stops with exit 7 (`NOT_READY`) naming the tool, the step that needed it and what was found. The table lists the tools this package runs itself and the minimum a plan in this build asks for:

<!-- BEGIN GENERATED: tool-requirements (tools/gen_tool_docs.py) -->
| Tool | Minimum | Purpose | Needed for | Install |
|---|---|---|---|---|
| `age` | 1.3 | encrypts the Archive under a passphrase (CLI passphrase backend) | the encrypted payload (ENCRYPT, VERIFY) | https://github.com/FiloSottile/age#installation |
| `age-plugin-batchpass` | — | supplies the passphrase to age non-interactively (CLI backend) | the encrypted payload (ENCRYPT, VERIFY) | https://github.com/FiloSottile/age/releases |
| `wp` | 2.12.0 | WP-CLI, which relocation runs to rewrite the Site's URLs | SEARCH_REPLACE: serialization-safe content URL rewrite for --new-site-url/--new-site-home-url (FR-12) | https://wp-cli.org/#installing |
| MySQL/MariaDB client tools | client 8.4+, MariaDB 11.4+ | the database dump and load | checked per Site by l3io-wordpress-database (`check_dump`/`check_load`), never run by this package | your distribution's MySQL or MariaDB client package |
<!-- END GENERATED: tool-requirements (tools/gen_tool_docs.py) -->

The `l3io-age-passphrase` binder, a required dependency, is published as wheels for Linux (glibc and musl; x86_64, aarch64, armv7l), macOS (x86_64, arm64) and Windows (amd64). On any other platform the installer builds it from its source distribution, which needs a Rust toolchain; if that build fails, the package does not install. When the binder is not importable, the passphrase backend falls back to the `age` command line (`L3IO_AGE_PASSPHRASE_BACKEND=auto`, the default), so `age` 1.3 or newer and `age-plugin-batchpass` must be on `PATH`, or an encrypted Run stops with exit 7 (`NOT_READY`) before its first effect. `L3IO_AGE_PASSPHRASE_BACKEND=cli` selects that backend explicitly.

## Usage

### Quick start: unattended, with no flags

Every option is also an `L3IO_WP_BACKUP_<NAME>` variable (`--wp-dir` is
`L3IO_WP_BACKUP_WP_DIR`) and a key of a TOML config file (`wp_dir = "..."`),
so a scheduled job needs no flags. A flag wins over a variable, a variable over
the file, the file over the default. The file is `--config PATH` or
`L3IO_WP_BACKUP_CONFIG`, else `/etc/l3io-wp-backup/config.toml` and then
`~/.config/l3io-wp-backup/config.toml` when present; one holding a secret
(`passphrase`, `db_password`, `admin_password`, `new_db_password`) must be
readable by its owner only (`chmod 600`), or it is refused. ADR-0028 records
the design.

**As a Docker sidecar**: the official image, below.

**From cron**, with the tool installed by `pip`:

```toml
# ~/.config/l3io-wp-backup/config.toml   (chmod 600)
wp_dir = "/var/www/example"
archive_dir = "/var/backups/example"
encrypt_with_passphrase = true
passphrase = "a long passphrase, 12 characters or more"
log_format = "console"
```

```
15 3 * * *  wpbackup2 backup
```

Without `--archive`, the Archive is named
`wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz` (UTC, so a site's Archives sort by
time), `<site>` being `--site-name` (`L3IO_WP_BACKUP_SITE_NAME`, `site_name`)
or else the database name, kept to `A-Z a-z 0-9 . _ -` (other characters
become `-`). Before 1.1.0 it was `wpbackup2-<db>-<YYYYmmddHHMMSS>.tar.gz`;
`--archive latest` finds Archives by their Manifest, so both names are found.
To sort Archives into subfolders, set `--archive-subdir TEMPLATE`
(`L3IO_WP_BACKUP_ARCHIVE_SUBDIR`, `archive_subdir`): a relative path under the
Archive directory using `{site}`, `{yyyy}`, `{mm}` and `{dd}` (the UTC date of
the name's timestamp), such as `{site}/{yyyy}/{mm}`; missing folders are made
`0700`. Unset, the Archive directory stays flat; `none` means flat too, so
`--archive-subdir none` turns off, for one Run, a template set in the
environment or a config file (`None` or `NONE` is refused as ambiguous). A bad
template is refused with its reason from every spelling. `--archive latest`
and `prune` search the Archive directory and its subfolders (up to 6 levels,
never through a symlinked folder), so changing the layout strands no Archive;
`prune --whatIf` lists the folders a prune would remove.
The Archive's payload is gzip at level 6 unless `--compression-level N`
(`L3IO_WP_BACKUP_COMPRESSION_LEVEL`, `compression_level`) sets 1 (fastest) to
9 (smallest); `restore --compression-level` sets the Safety Snapshot's. Before
1.1.1 it was always 9: on a 1.1 GiB test site, level 6 wrote the Archive
1.7 times faster for 0.2% more bytes, level 1 2.7 times faster for 5.6% more.
The Archive format is the same at every level, so any version reads any of
them.
Without `--archive` or `archive_dir`, the Archive goes into the current
directory, or into the per-user data directory
(`~/.local/share/l3io-wp-backup/archives`) when the current directory is
inside the site, never inside the Site Root. A target you put inside the Site
Root is written with a warning: the web server may serve it. A backup started
as root runs as the Site Root's owner: that user's data directory, and an
Archive directory that user can write (otherwise the backup is refused before
it starts, naming the directory).

### Run as a Docker sidecar

The official image runs next to the official `wordpress` image, sharing its
site volume and its `WORDPRESS_DB_*` environment. It is built from the PyPI
release, for `linux/amd64` and `linux/arm64`:

```
docker pull ghcr.io/liquidlogiclabs/l3io-wordpress-backup:1           # MySQL servers
docker pull ghcr.io/liquidlogiclabs/l3io-wordpress-backup:1-mariadb   # MariaDB servers
docker pull docker.io/ravensorb/l3io-wordpress-backup:1               # the same images
docker pull docker.io/ravensorb/l3io-wordpress-backup:1-mariadb
```

Pick the variant by the database server: `:1-mariadb` (MariaDB's client) for
MariaDB, `:1` (MySQL's client) for MySQL 8.0 and 8.4. The default dumps
MariaDB too, but logs `client_server_skew` on every dump; the `-mariadb`
variant does not, and does not work against MySQL
([docs/docker.md](docs/docker.md#which-variant)). By default
it is a scheduler: a daily `wpbackup2 backup` at 03:00 (`WPBACKUP_SCHEDULE`, in
`TZ`), run as uid/gid 33 (www-data; `PUID`/`PGID`, or compose `user:`, to
change), each job's output in the container log. Given a command, it runs that
once instead. **The image never changes the owner of anything you mount**: the
Archive and state folders must be that user's (new named volumes, as below,
are 33's already). A folder it cannot use stops the scheduler at start, and
any Run, with the path, its owner and mode, and the `chown` to run
([docs/docker.md](docs/docker.md#ownership)).

```yaml
services:
  db:
    image: mariadb:11.4
    env_file: db.env                 # MARIADB_DATABASE, _USER, _PASSWORD, _ROOT_PASSWORD
    volumes: [db:/var/lib/mysql]

  wordpress:
    image: wordpress:php8.3-apache
    env_file: wordpress.env          # WORDPRESS_DB_HOST=db, _NAME, _USER, _PASSWORD (or _PASSWORD_FILE)
    volumes: [html:/var/www/html]
    depends_on: [db]

  backup:
    image: ghcr.io/liquidlogiclabs/l3io-wordpress-backup:1-mariadb   # a MariaDB server; :1 for MySQL
    env_file: wordpress.env          # the site's own WORDPRESS_DB_*
    environment:
      WPBACKUP_SCHEDULE: "0 3 * * *" # the default: daily at 03:00
      TZ: Europe/London
      L3IO_WP_BACKUP_CONFIG: /run/secrets/wpbackup.toml
    secrets: [wpbackup.toml]
    volumes:
      - html:/var/www/html                     # L3IO_WP_BACKUP_WP_DIR
      - backups:/backups                       # L3IO_WP_BACKUP_ARCHIVE_DIR
      - state:/var/lib/wpbackup/state          # the Site Lock and the Run Journal
    tmpfs:
      - /tmp/wpbackup:mode=0700,uid=33,gid=33  # the Dump never touches disk
    restart: unless-stopped
    depends_on: [wordpress]

secrets:
  wpbackup.toml:
    file: ./wpbackup.toml   # chown 33:33, chmod 600: a file holding a secret must be its owner's only

volumes: {db: {}, html: {}, backups: {}, state: {}}
```

```toml
# wpbackup.toml
encrypt_with_passphrase = true
passphrase = "a long passphrase, 12 characters or more"
```

Restore the newest backup of this site, with no other flag, either in the
running container or in a one-shot one:

```
docker compose exec backup wpbackup2 restore --archive latest
docker compose run --rm backup restore --archive latest
```

An Archive written by another uid than the one owning `/var/www/html` (a
restore onto a new host) needs root to start: `docker compose run --rm -e
PUID=0 backup restore --archive latest`. `wpbackup2` then runs as the Site
Root's owner, so the restored files are the site's; the state volume must be
that owner's too ([docs/docker.md](docs/docker.md#restoring-an-archive-another-user-wrote)).

After each successful scheduled backup the image prunes this site's older
Archives with tiered retention: the newest of each of the last 7 days, 4 ISO
weeks and 6 months that have one (`WPBACKUP_KEEP_DAILY`, `_WEEKLY`, `_MONTHLY`;
also `WPBACKUP_KEEP_YEARLY` and `WPBACKUP_KEEP` for the newest N; all unset or
`0` disables pruning). An Archive kept by any rule stays, and the newest always
does: [how retention works](docs/docker.md#how-retention-works). By hand,
anywhere: `wpbackup2 prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6`,
or `--keep 14` for the newest 14 (`--whatIf` lists what it would delete, and
which rule keeps each Archive). Only Archives whose Manifest is this site's
are ever deleted, never another site's, a Safety Snapshot or a file that is no
Archive. With Archive subfolders (`L3IO_WP_BACKUP_ARCHIVE_SUBDIR='{site}/{yyyy}'`
in the service's environment), prune searches them all and removes the
folders it empties.

To schedule from the host instead (cron or a systemd timer), run the one-shot
form, `docker compose run --rm backup backup`, and put the service in a
profile (`profiles: [manual]`) so `docker compose up` does not also start the
built-in scheduler. More jobs: mount a crontab at `/etc/l3io-wp-backup/crontab`; it is
added to `WPBACKUP_SCHEDULE`'s line. [docs/docker.md](docs/docker.md) has the
details: users and `PUID`/`PGID`, read-only root filesystems, tags, what is in
the image (no WP-CLI: a `--new-site-url` relocation stops with exit 7), and the
release pipeline. `tests/sidecar/` runs this setup, both modes, on every engine.

### By hand

To backup:

```
wpbackup2 backup --wp-dir /www/wordpress --archive ~/backup.tar.gz
```

To rehearse a backup first, add `--whatIf` (before or after `backup`). The Dry Run resolves the credentials, runs the preflight and asks the database whether it can be dumped, all for real; then it prints the ordered plan and writes nothing. Its last line says so. It does not detect what only writing finds: a full disk, a file that cannot be read, or an Archive larger than the Manifest cap. A Site Lock is taken as for a real Run, so a Dry Run waits for nobody and is refused (exit 9) while another Run of the same Site by the same OS user holds the lock.

**Output for cron mail.** stderr is the log stream. On a terminal it is human-readable; anywhere else (cron, a pipe, journald) it is strict JSON lines, one object per line, so a log collector can parse every line. Where a person reads the output instead — a cron job that mails it — pass `--log-format console` before the subcommand to get human-readable lines there too, failures and warnings included:

```
wpbackup2 --log-format console backup --wp-dir /www/wordpress --archive ~/backup.tar.gz
```

To check an archive without touching a site:

```
wpbackup2 verify --archive ~/backup.tar.gz
```

Without a key, `verify` proves the archive's checksums are **consistent** with its manifest, not that the archive is **authentic**: the manifest is unprotected, so anyone who can rewrite the archive can rewrite its checksums too. It says so (`authenticity not checked (no key)`). For an encrypted archive, `--authenticate` decrypts the whole payload with its passphrase — taken from `L3IO_WP_BACKUP_PASSPHRASE` or a terminal prompt, never from the command line — and `age` authenticates every chunk. A wrong passphrase or a tampered payload exits 8; a plain archive is refused, since there is nothing to authenticate. **Encryption protects file contents only.** The manifest stays readable in clear in an encrypted archive: the site path, the database name, every file path and size, and the exclusions are visible to anyone who can read the archive, without the passphrase. To refuse a plain archive where an encrypted one is expected (for example one substituted by someone who can write the storage target), restore with `--require-encrypted`; without it a plain archive is restored with a warning, since plain archives are not authenticated.

**Memory when reading an archive.** `verify` and `restore` read the whole manifest into memory and parse it before anything is authenticated, taking about 4.3 times its size. A manifest is capped at 256 MiB, so the worst case is about 1.1 GB; an ordinary site's manifest is a few MB. A streaming manifest reader is planned after v1.

To restore (see [docs/restore.md](docs/restore.md) for every mode, the refusals and their exit codes):

```
wpbackup2 restore --wp-dir /www/wordpress --archive ~/backup.tar.gz --mode all
```

A restore takes a Safety Snapshot of whatever it would change first. A database restore then loads the Dump straight into the live database, after dropping every table that carries the site's table prefix (tables without it are never touched), and reloads the Safety Snapshot automatically if the load fails; see [docs/restore.md](docs/restore.md#the-database-loaded-straight-into-the-live-database). Over a Site Root that is not empty and does not look like WordPress (none of `wp-config.php`, `wp-includes/`, `wp-content/`), or a database with tables none of which carries the site's prefix, it refuses unless `--force` is given (exit 3, naming the flag). `--archive latest` restores the newest Archive of this site in `--archive-dir` (see [docs/restore.md](docs/restore.md#--archive-latest)). A target with no `wp-config.php` takes its database from an encrypted archive's config as it says, and from an unencrypted one only when you confirm it with `--archive-database USER@HOST[:PORT]/NAME`. Database connections require no TLS unless you ask with `--db-tls {required,verify-ca,verify-identity}` (and `--db-tls-ca FILE`). Started as root, a restore (like a backup) runs as the Site Root's owner. The library (`api.backup`, `api.restore`) refuses to run as root: it never switches its host's user.

`wp-config.php` is read for your database credentials. It is looked for in the WordPress directory, then its parent; pass `--wp-config PATH` to use another.

### Credentials

No credential has to be on the command line, so a cron line needs none. Each account's credentials come from the first source that has them, in a fixed order:

| Account | 1. Explicit | 2. Environment | 3. `wp-config.php` |
|---|---|---|---|
| WordPress database (backup) | `--db-user`/`--db-password`, their config-file keys, or library `BackupRequest.credentials` | `L3IO_WP_BACKUP_DB_USER` and `L3IO_WP_BACKUP_DB_PASSWORD` | `DB_USER`/`DB_PASSWORD` |
| WordPress database (restore) | none | none | `DB_USER`/`DB_PASSWORD`, the account the restored site will use |
| Administrator (restore) | `--admin-user` (or `--admin-username`)/`--admin-password`, their config-file keys, or library `RestoreRequest.admin` | `L3IO_WP_BACKUP_ADMIN_USER` and `L3IO_WP_BACKUP_ADMIN_PASSWORD` | never |

- **Both variables of a pair, or neither.** Setting one without its partner is an error (exit 4), not a fall-through to `wp-config.php`. A variable set to an empty value counts as set. The one exception is `--admin-username` without `--admin-password`: the password then comes from `L3IO_WP_BACKUP_ADMIN_PASSWORD`, and an empty value there counts as unset. The administrator pair is read only by a restore that needs an administrator, so `--mode files` never asks for one, and neither does a database restore whose WordPress account can load its own database (owner decision 55): an administrator is needed only when the database does not exist yet or the account cannot log in or lacks a right on it.
- **A source that fails stops the search.** An unreadable AWS secret, for example, never becomes the next source's account. The administrator is never taken from `wp-config.php` or from the WordPress account. A restore that needs one and has none is refused before anything changes (exit 4), naming the database, the account and what is missing.
- **AWS Secrets Manager.** A secret holding the JSON `{"username": ..., "password": ...}` can supply either account as its explicit source. Install the `aws` extra: `pip install 'l3io-wordpress-backup[aws]'`. Without it, exit 7 names the extra. Without a region from the request or the AWS session, exit 4 names the region flag. In this release only the library selects it: `AwsSecretCredentialSpec(secret_id, region)` as `BackupRequest.credentials` or `RestoreRequest.admin`. The `--db-credentials-aws-*` and `--admin-credentials-aws-*` flags are accepted but not implemented yet.
- **`getenv_docker()` and `getenv()` in `wp-config.php`.** The official `wordpress` Docker image defines `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` and `$table_prefix` as `getenv_docker('WORDPRESS_DB_HOST', 'mysql')` and so on. Backup, `doctor` and restore resolve them as PHP would, from their own environment: the variable, a Docker secret named by `<NAME>_FILE`, else the default. Run the tool with the site's `WORDPRESS_DB_*` (a sidecar container can share the WordPress container's environment file); the resolved host is logged (`database_host`) and named in the Dry Run. Any other PHP expression in those settings is refused (exit 3) without being evaluated. A database restore loads straight into the database the environment names and leaves `wp-config.php` as it is (owner decision 49); only a relocation flag (`--new-db-*`) that would rewrite such a constant is refused (exit 3). `--overwrite-wp-config` keeps those definitions as written, and is refused (exit 3) when the Archive's `wp-config.php` does not define `getenv_docker()`: see [docs/restore.md](docs/restore.md#wp-configphp).
- **The log names the source, never the value.** Each resolution logs `credential_source_selected` with the source's label, for example `environment:L3IO_WP_BACKUP_DB_USER,L3IO_WP_BACKUP_DB_PASSWORD`.

The environment of a process can be read by the same user and by root (`/proc/<pid>/environ`), and a secret flag (`--passphrase`, `--db-password`, `--admin-password`, `--new-db-password`) is visible to every user in `ps` and logs WARNING `password_on_command_line`. On a shared host, prefer `wp-config.php`, a `chmod 600` config file or AWS Secrets Manager. A database password, or a passphrase that decrypts (`verify --authenticate`, `restore`), shorter than 4 characters is accepted with WARNING `credential_below_redaction_floor`: it is too short to be masked in logs. A passphrase that encrypts needs 12 characters or more.

### Command-line reference

Generated from the parser by `tools/gen_cli_docs.py`; CI fails if it drifts.

<!-- BEGIN GENERATED: cli-usage (tools/gen_cli_docs.py) -->
```text
$ wpbackup2 --help
usage: wpbackup2 [-h] [--version] [--config PATH]
                 [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--log-format {console,json}]
                 [--whatIf]
                 {backup,restore,verify,doctor,prune} ...

Backup/restore your WordPress content and database.

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

positional arguments:
  {backup,restore,verify,doctor,prune}
                        Action being requested
    backup              Perform a WordPress backup
    restore             Perform a WordPress restore
    verify              Check an archive without touching a site
    doctor              Report whether this host is ready to back up and restore
    prune               Delete this site's older archives, keeping what --keep N and the --keep-
                        daily/-weekly/-monthly/-yearly tiers keep

options:
  -h, --help            Show this help message and exit
  --version             Show the program's version number and exit
  --config PATH         Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                        /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                        backup/config.toml). A file holding a secret must be readable by its owner
                        only
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Logging level (stdlib level names; default INFO)
  --log-format {console,json}
                        Log format on stderr (default: console on a terminal, else json)
  --whatIf, --what-if   Dry Run: resolve credentials, run preflight and every query for real, and
                        print the ordered plan; changes no Site, database or archive, but takes
                        the Site Lock (exit 9 LOCKED while another Run holds it)

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
usage: wpbackup2 backup [-h] [--config PATH] [--wp-dir WP_DIR] [--archive-dir DIR] [--whatIf]
                        [--archive ARCHIVE] [--site-name NAME] [--archive-subdir TEMPLATE]
                        [--wp-config PATH] [--mode {files,db,all}] [--encrypt-with-passphrase]
                        [--passphrase SECRET] [--exclude PATTERN] [--no-default-excludes]
                        [--compression-level N] [--list-default-excludes]
                        [--db-credentials-aws-secret-id ID] [--db-credentials-aws-region REGION]
                        [--db-user USER] [--db-password SECRET]
                        [--db-tls {required,verify-ca,verify-identity}] [--db-tls-ca FILE]

Perform a WordPress backup

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

options:
  -h, --help            Show this help message and exit
  --config PATH         Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                        /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                        backup/config.toml). A file holding a secret must be readable by its owner
                        only
  --wp-dir WP_DIR       Path to the root of the WordPress directory (required, except with --list-
                        default-excludes)
  --archive-dir DIR     Directory the archive is written to when --archive is not given (default:
                        the current directory, or the per-user data directory when the current
                        directory is inside the Site Root)
  --whatIf, --what-if   Dry Run: run every check and query for real and print the ordered plan;
                        writes no archive but takes the Site Lock (also accepted before `backup`)
  --archive ARCHIVE     Archive path or file: URL (default: wpbak-<site>-<UTC timestamp>.tar.gz in
                        --archive-dir, the timestamp as YYYYMMDDTHHMMSSZ). Inside the Site Root it
                        is written with a warning: the web server may serve it
  --site-name NAME      The <site> part of the default archive name (default: the database name);
                        characters other than A-Z, a-z, 0-9, '.', '_' and '-' become '-'
  --archive-subdir TEMPLATE
                        Write the default-named archive into this subfolder of --archive-dir, made
                        0700 when missing (default: none, flat; 'none' turns off a template set in
                        the environment or a config file). A relative path using {site} (the
                        archive name's <site>), {yyyy}, {mm} and {dd} (the UTC date of the archive
                        name's timestamp), e.g. '{site}/{yyyy}/{mm}'; at most 6 levels. Ignored
                        with --archive. restore --archive latest and prune search subfolders
  --wp-config PATH      Use this wp-config.php instead of searching for it
  --mode {files,db,all}
                        What to back up: files, db, or all (default: all)
  --encrypt-with-passphrase
                        Encrypt the archive with a passphrase (--passphrase,
                        L3IO_WP_BACKUP_PASSPHRASE, the config file or a prompt)
  --passphrase SECRET   Passphrase that encrypts the archive (with --encrypt-with-passphrase). The
                        value is visible in the process list; prefer L3IO_WP_BACKUP_PASSPHRASE or
                        the config file (chmod 600)
  --exclude PATTERN     Leave matching paths out of the archive (repeatable)
  --no-default-excludes
                        Do not apply the default exclusion set
  --compression-level N
                        Gzip level of the archive's payload, 1 (fastest) to 9 (smallest) (default:
                        6). The archive format is the same at every level
  --list-default-excludes
                        Print the default exclusion set and exit without backing up (needs no
                        --wp-dir; takes no other backup flag)

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id)
  --db-user USER        WordPress database account to dump as, instead of wp-config.php's (give
                        --db-password with it)
  --db-password SECRET  Password of --db-user. The value is visible in the process list; prefer
                        L3IO_WP_BACKUP_DB_PASSWORD or the config file (chmod 600)
  --db-tls {required,verify-ca,verify-identity}
                        Require TLS on every database connection, at this level (default: no
                        requirement, as WordPress connects)
  --db-tls-ca FILE      CA bundle that verifies the database server, with --db-tls verify-ca or
                        verify-identity (default: the system store)

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
$ wpbackup2 restore --help
usage: wpbackup2 restore [-h] [--config PATH] --wp-dir WP_DIR --archive ARCHIVE
                         [--archive-dir DIR] [--mode {files,db,all}] [--cleanfirst]
                         [--drop-database] [--force] [--whatIf] [--wp-config PATH]
                         [--overwrite-wp-config] [--reset-existing-db-user] [--no-safety-snapshot]
                         [--require-encrypted] [--passphrase SECRET] [--snapshot-dir PATH]
                         [--compression-level N] [--archive-database USER@HOST[:PORT]/NAME]
                         [--db-tls {required,verify-ca,verify-identity}] [--db-tls-ca FILE]
                         [--admin-tls {required,verify-ca,verify-identity}] [--admin-tls-ca FILE]
                         [--resolve-interrupted-run RUN_ID] [--admin-user USER]
                         [--admin-password SECRET] [--admin-credentials-aws-secret-id ID]
                         [--admin-credentials-aws-region REGION] [--only-appointed-in-asg]
                         [--new-site-url URL] [--new-site-home-url URL] [--new-db-host HOST]
                         [--new-db-name NAME] [--new-db-user USER] [--new-db-password SECRET]
                         [--new-db-password-file PATH] [--allow-unencrypted]
                         [--db-credentials-aws-secret-id ID] [--db-credentials-aws-region REGION]

Perform a WordPress restore

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

options:
  -h, --help            Show this help message and exit
  --config PATH         Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                        /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                        backup/config.toml). A file holding a secret must be readable by its owner
                        only
  --wp-dir WP_DIR       Path to the root of the WordPress directory
  --archive ARCHIVE     Path or file: URL of the archive to restore from, or `latest`: the newest
                        archive of this site in --archive-dir (required, --resolve-interrupted-run
                        included)
  --archive-dir DIR     Directory --archive latest searches (default: the current directory, or
                        the per-user data directory when the current directory is inside the Site
                        Root)

restore mode:
  Together these select one Restore Mode; any other combination is refused.

  --mode {files,db,all}
                        What to restore: files, db, or all (default: all)
  --cleanfirst          Clear the Site Root before restoring the files. The database is replaced
                        by every restore that loads it
  --drop-database       Deprecated; has no effect. A database restore loads into the live database
                        and drops the tables with the site's prefix first, so no previous database
                        is left to remove
  --force               Restore over a non-empty Site Root that does not look like WordPress (none
                        of wp-config.php, wp-includes/, wp-content/), or a non-empty database none
                        of whose tables has the site's table prefix. It selects no mode and
                        removes nothing

restore options:
  The flags that do not select the Restore Mode: Dry Run, wp-config.php, the Safety Snapshot, the database administrator and relocation

  --whatIf, --what-if   Dry Run: run every check and query for real and print the ordered plan;
                        changes no Site or database but takes the Site Lock (also accepted before
                        `restore`)
  --wp-config PATH      Use this wp-config.php instead of searching for it
  --overwrite-wp-config
                        Replace the target's existing wp-config.php with the archive's (only with
                        a mode that restores files), keeping the target's DB_* definitions as
                        written, getenv_docker(...) included. Refused (exit 3) when the target
                        uses getenv_docker(...) and the archive's wp-config.php does not define it
  --reset-existing-db-user
                        When the target has no wp-config.php and the archive's database account
                        already exists: reset its password to the archive's (refused otherwise)
  --no-safety-snapshot  Do not take the Safety Snapshot before changing anything
  --require-encrypted   Refuse an archive that is not encrypted, before anything changes (guards
                        against a plain archive substituted for an encrypted one; without it a
                        plain archive is restored with a warning)
  --passphrase SECRET   Passphrase that decrypts the archive and encrypts the Safety Snapshot. The
                        value is visible in the process list; prefer L3IO_WP_BACKUP_PASSPHRASE or
                        the config file (chmod 600)
  --snapshot-dir PATH   Directory to write the Safety Snapshot to (default: the per-user state
                        directory). Refused inside the Site Root or the wp-config.php directory; a
                        directory a web server serves from elsewhere is not detected
  --compression-level N
                        Gzip level of the Safety Snapshot's payload, 1 (fastest) to 9 (smallest)
                        (default: 6). The archive format is the same at every level
  --archive-database USER@HOST[:PORT]/NAME
                        When the target has no wp-config.php and the archive is not encrypted:
                        confirm the database the archive's config names (user, host, port and name
                        must all match), or the restore is refused
  --db-tls {required,verify-ca,verify-identity}
                        Require TLS on every database connection, at this level (default: no
                        requirement, as WordPress connects)
  --db-tls-ca FILE      CA bundle that verifies the database server, with --db-tls verify-ca or
                        verify-identity (default: the system store)
  --admin-tls {required,verify-ca,verify-identity}
                        Deprecated: use --db-tls
  --admin-tls-ca FILE   Deprecated: use --db-tls-ca
  --resolve-interrupted-run RUN_ID
                        Resolve the journal left by the interrupted Run with this ID, then stop:
                        nothing is restored (run the restore again afterwards)
  --admin-user, --admin-username USER
                        Database administrator username (give --admin-password with it, or set
                        L3IO_WP_BACKUP_ADMIN_PASSWORD)
  --admin-password SECRET
                        Database administrator password. The value is visible in the process list;
                        prefer L3IO_WP_BACKUP_ADMIN_PASSWORD or the config file (chmod 600)
  --admin-credentials-aws-secret-id ID
                        Read the database administrator from this AWS Secrets Manager secret
  --admin-credentials-aws-region REGION
                        AWS region of --admin-credentials-aws-secret-id (needs the secret id)
  --only-appointed-in-asg
                        Removed; accepted only to say so (not available in this build)
  --new-site-url URL    Relocate the Site to this URL
  --new-site-home-url URL
                        Relocate the Site's home URL
  --new-db-host HOST    Relocate to this database host
  --new-db-name NAME    Relocate to this database name; it must be missing (created, and dropped
                        again if the restore fails) or empty, --force or not
  --new-db-user USER    Relocate to this database user
  --new-db-password SECRET
                        Password of --new-db-user. The value is visible in the process list;
                        prefer --new-db-password-file, L3IO_WP_BACKUP_NEW_DB_PASSWORD or the
                        config file (chmod 600)
  --new-db-password-file PATH
                        Read the password of --new-db-user from this file (its first line; make it
                        readable only by you)
  --allow-unencrypted   Deprecated, no effect: a plain archive restored with a passphrase proceeds
                        with a warning; --require-encrypted refuses it

database credentials:
  --db-credentials-aws-secret-id ID
                        Read the WordPress database account from this AWS Secrets Manager secret
  --db-credentials-aws-region REGION
                        AWS region of --db-credentials-aws-secret-id (needs the secret id)

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
$ wpbackup2 verify --help
usage: wpbackup2 verify [-h] [--config PATH] [--whatIf] --archive ARCHIVE [--authenticate]
                        [--passphrase SECRET]

Check an archive without touching a site

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

options:
  -h, --help           Show this help message and exit
  --config PATH        Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                       /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                       backup/config.toml). A file holding a secret must be readable by its owner
                       only
  --whatIf, --what-if  Accepted for symmetry: verify is read-only, so it changes nothing
  --archive ARCHIVE    Path or file: URL of the archive to check
  --authenticate       Decrypt the encrypted payload to prove the archive authentic; without it,
                       verify proves only that its checksums are consistent. The passphrase comes
                       from --passphrase, L3IO_WP_BACKUP_PASSPHRASE, the config file or a prompt.
                       It does not show the archive is the latest one: an older archive under the
                       same passphrase also passes
  --passphrase SECRET  Passphrase that decrypts the archive (with --authenticate). The value is
                       visible in the process list; prefer L3IO_WP_BACKUP_PASSPHRASE or the config
                       file (chmod 600)

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
$ wpbackup2 doctor --help
usage: wpbackup2 doctor [-h] [--config PATH] [--wp-dir DIR] [--archive TARGET] [--json]

Report whether this host is ready to back up and restore

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

options:
  -h, --help        Show this help message and exit
  --config PATH     Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                    /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                    backup/config.toml). A file holding a secret must be readable by its owner
                    only
  --wp-dir DIR      Also check this Site Root
  --archive TARGET  Also check this archive target
  --json            Print the diagnosis as JSON

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
$ wpbackup2 prune --help
usage: wpbackup2 prune [-h] [--config PATH] --wp-dir WP_DIR [--keep N] [--keep-daily N]
                       [--keep-weekly N] [--keep-monthly N] [--keep-yearly N] [--archive-dir DIR]
                       [--wp-config PATH] [--whatIf]

Delete this site's older archives, keeping what --keep N and the --keep-daily/-weekly/-monthly/-yearly tiers keep

Every option can also be set as an L3IO_WP_BACKUP_<NAME> variable
(--wp-dir is L3IO_WP_BACKUP_WP_DIR) or as a key in a TOML config file
(wp_dir = "..."); a flag wins over a variable, a variable over the file.

options:
  -h, --help           Show this help message and exit
  --config PATH        Read settings from this TOML file (also L3IO_WP_BACKUP_CONFIG; default:
                       /etc/l3io-wp-backup/config.toml, then the per-user ~/.config/l3io-wp-
                       backup/config.toml). A file holding a secret must be readable by its owner
                       only
  --wp-dir WP_DIR      Path to the root of the WordPress directory whose archives are pruned
  --keep N             Keep this site's newest N archives (at least 1). At least one of --keep and
                       the --keep-<tier> rules is required; an archive kept by any rule stays, the
                       newest always, and this site's others in --archive-dir are deleted. Only
                       archives whose manifest is this site's count; safety snapshots, other
                       sites' archives and unreadable files are never deleted
  --keep-daily N       Keep the newest archive of each UTC day, for the N most recent days that
                       have one (at least 1), by Manifest created_at
  --keep-weekly N      Keep the newest archive of each ISO week (Monday to Sunday, UTC), for the N
                       most recent weeks that have one (at least 1), by Manifest created_at
  --keep-monthly N     Keep the newest archive of each calendar month (UTC), for the N most recent
                       months that have one (at least 1), by Manifest created_at
  --keep-yearly N      Keep the newest archive of each calendar year (UTC), for the N most recent
                       years that have one (at least 1), by Manifest created_at
  --archive-dir DIR    Directory holding the archives (default: the one a backup writes to by
                       default)
  --wp-config PATH     Use this wp-config.php instead of searching for it
  --whatIf, --what-if  Dry Run: list the archives that would be deleted and delete none; takes the
                       Site Lock (also accepted before `prune`)

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
<!-- END GENERATED: cli-usage -->

### Exit codes

A scheduler can branch on the exit code alone: `0` means the Run succeeded and
every failure exits non-zero with a code naming its kind. The table is
append-only — a code is never renumbered or reused. Ctrl-C exits with the
shell's `130` (`INTERRUPTED` in the log), which is not part of the table.

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
