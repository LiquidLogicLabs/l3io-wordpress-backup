# Restore

How `wpbackup2 restore` puts an Archive back onto a Site, and what it refuses.
Every flag, `--whatIf` included, is listed by `wpbackup2 restore --help`.
`--whatIf` (or `--what-if`) may be given before or after `restore`:
`wpbackup2 --whatIf restore ...` and `wpbackup2 restore --whatIf ...` are the
same Dry Run.

## Terms

The capitalised words below have one meaning throughout this page (UX-9).

- **Site** — one WordPress installation: its files and its database.
- **Site Root** — the directory WordPress is installed in;
  set by `--wp-dir`.
- **Archive** — the file a backup writes and a restore reads (`--archive`).
- **Dump** — the database's SQL contents inside an Archive.
- **Restore Mode** (or Mode) — what a restore puts back and what it may remove:
  files, the database or both (`--mode`, `--cleanfirst`; see the table below).
- **Run** — one invocation of `wpbackup2`, named by its Run ID (a UUID in
  every log record, `l3io_run_id`).
- **Dry Run** — a Run with `--whatIf`: it checks and prints, and changes nothing.
- **Run workspace** — the Run's private scratch directory (mode `0700`),
  `l3io-wp-backup-*` under `$TMPDIR`, removed when the Run ends; the Archive's
  copy, a decrypted payload and the Dump are held there, never in the Site Root.
- **Safety Snapshot** — the backup a restore takes of what it is about to
  replace, before the first destructive step; written to `--snapshot-dir`, by
  default the per-user state directory (below).
- **Run Journal** — the restore's record of each phase it started and
  completed, so a Run killed part-way is detected and resolved
  (`--resolve-interrupted-run`).
- **Site Lock** — the lock that stops two Runs of the same Site at once
  (exit 9 `LOCKED`).
- **Per-user state directory** — where the Run Journal, the Site Lock and the
  default Safety Snapshots live: `$XDG_STATE_HOME/l3io-wp-backup`, or
  `~/.local/state/l3io-wp-backup` when `XDG_STATE_HOME` is unset (on Linux).
  The journal and snapshots of one Site are under
  `snapshots/<SHA-256 of its resolved Site Root path>/`, the locks under `locks/`.
- **Phase** — one step of a restore, logged in upper case (`PREFLIGHT`,
  `VERIFY`, `SNAPSHOT`, `CLEAN`, `EXTRACT`, `REWRITE_CONFIG`, `LOAD_DUMP`,
  `SEARCH_REPLACE`).

## Restore Modes

The Mode is resolved once from the flags and governs the whole Run. A Mode that
does not name file removal removes no files.

| Mode             | Flags                         | Clears the Site Root | Writes files | Loads the Dump |
|------------------|-------------------------------|----------------------|--------------|----------------|
| `files`          | `--mode files`                | no                   | yes          | no             |
| `files-clean`    | `--mode files --cleanfirst`   | yes                  | yes          | no             |
| `database`       | `--mode db`                   | no                   | no           | yes            |
| `database-clean` | `--mode db --drop-database`   | no                   | no           | yes            |
| `all`            | `--mode all`                  | no                   | yes          | yes            |
| `all-clean`      | `--mode all --cleanfirst`     | yes                  | yes          | yes            |

Every Mode that loads the Dump replaces the database's tables that carry the
site's table prefix (below), so the database matches the Archive. There is no
"previous database" left to remove, and `--drop-database` is **deprecated and
has no effect** (owner decision 63): it is accepted, so a script that passes
it keeps running, and logs WARNING `deprecated_option`. With `--mode db` it
still selects the Mode named `database-clean`, which restores exactly as
`database` does; with `--mode files` or `--mode all` it changes nothing.

`--mode all` is the default, so `--cleanfirst` alone selects `all-clean`. The
Mode names are what the log (`restore_mode`), refusals
("Restore Mode files") and the completion line use; the flags are what you
type. A library host passes the Mode and its overrides as values, without this
flag table: see [`api.md`](api.md#restore) before forwarding user input into
`RestoreMode` or `Override`. Note the one spelling difference: the flag value `db` is the Mode
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

## The database: loaded straight into the live database

A restore loads the Dump **straight into the live database** that
`wp-config.php` names (owner decisions 49 and 55, ADR-0027), as the site's own
WordPress account, after the Safety Snapshot. It does not rename the database
or rewrite `DB_NAME`: the site keeps running on the database it ran on, and a
`wp-config.php` whose `DB_*` come from the environment (the official
`wordpress` image's `getenv_docker()`) keeps following it.

In order, inside `LOAD_DUMP`:

1. The load checks run as the WordPress account first (at `PREFLIGHT`, before
   anything changes). An administrator is used **only** when the database
   does not exist yet or the account lacks a right on it (below).
2. Every table and view whose name starts with the site's table prefix
   (`$table_prefix`, `wp_` by default) is dropped, then the Dump is loaded.
   So after a restore the database holds exactly the Archive's tables with
   that prefix: a plugin's table that was added after the backup is gone
   (owner decisions 52 and 53). **Tables without the prefix are never
   touched**: another application's tables, or another WordPress sharing the
   database under another prefix. The tables dropped are logged by name
   (`database_tables_dropped`) and printed in the report.
3. The loaded database is checked: it must be a WordPress database with a
   schema version.
4. Only with a relocation (`--new-db-*`, below), the `wp-config.php` keys it
   moves are written, one journaled atomic write each.

**The site is down for the length of the load**: WordPress reads a database
that is being replaced. Restore when a short outage is acceptable.

**A failure rolls back automatically** (owner decision 50). If anything fails
after the load began (the load itself, the check, or a relocation key that did
not land), the Run reloads the Safety Snapshot into the live database the same
way (prefixed tables dropped, then its Dump loaded) before it exits, logs
WARNING `database_rolled_back`, and exits with the load's own error. The
database is then as it was before the Run. A database that held nothing
before is emptied again instead (its prefixed tables dropped, or the database
dropped if this Run created it).

If the rollback fails too, or nothing can roll back (`--no-safety-snapshot`
over a database that held tables), the Run exits 1 `FAILED` with
`RollbackFailedError`, which names both failures, the Safety Snapshot and the
commands that restore it, and the Run is recorded as interrupted, so the next
restore refuses until you resolve it:

```
wpbackup2 restore --wp-dir /srv/www/example --resolve-interrupted-run <run id>
wpbackup2 restore --wp-dir /srv/www/example --archive <safety snapshot> --mode db
```

A SIGTERM or Ctrl-C during the load is not rolled back: the Run stops at once
and is recorded as interrupted, and its undo text names the snapshot and the
same commands.

**The Dry Run shows what would be overwritten**: the `LOAD_DUMP` line names the
live database (host, port and name), every table that would be dropped first,
and what would roll it back; the `SNAPSHOT` line names where the snapshot would
be written. The host and database are also logged (`planned_action`), as a
backup logs its host (owner decision 51): no confirmation flag is asked for.

## Exit codes

| Exit | Name                  | A restore exits with it when                                                                 |
|------|-----------------------|----------------------------------------------------------------------------------------------|
| 0    | `OK`                  | it completed (a Dry Run: its plan completed without a refusal)                              |
| 1    | `FAILED`              | something failed, including a database load the report does not call restored, and a load whose automatic rollback failed (`RollbackFailedError`) |
| 2    | `USAGE`               | the flags express no Restore Mode, `--archive` is missing, or `--overwrite-wp-config` is given with a Mode that restores no files |
| 3    | `REFUSED`             | a check refused: a non-empty target, an incomplete earlier Run, an unreadable Site Root, an unconfirmed Archive database, a missing Dump, an encrypted Archive with no passphrase, a snapshot that could not be made, a relocation (`--new-db-*`) that would rewrite a `DB_*` read from the environment, a `DB_*` that is another PHP expression, an `--overwrite-wp-config` whose Archive's file does not define `getenv_docker()` for the target's definitions, an encrypted payload whose scrypt work factor is above this host's limit (`WorkFactorTooHighError`) |
| 4    | `CREDENTIALS`         | the database does not exist yet or the WordPress account cannot log in or lacks a right on it, and no administrator credentials are configured (`AdminCredentialsRequiredError`, naming what is missing); or a credential source is unusable |
| 5    | `UNREACHABLE`         | the database server cannot be reached                                                       |
| 7    | `NOT_READY`           | a tool the plan needs is absent or too old, an optional extra is not installed or the installation is incomplete (`IncompleteInstallError`), the Site Lock's state directory is unusable, or the database's load check, run as the WordPress account, could not run (`LoadReadinessError`, naming that account's credential source) |
| 8    | `VERIFICATION_FAILED` | the Archive is malformed or corrupt, or a passphrase is wrong                                |
| 9    | `LOCKED`              | another Run holds this Site's lock                                                          |

## Non-empty targets

A restore never overwrites something it was not told it may. Before any phase
is planned, each part the Mode restores is checked:

- **files** — the Site Root holds any entry **and looks like no WordPress
  site**: it has none of `wp-config.php`, `wp-includes/` or `wp-content/`
  (owner decision 57). Restoring over a recognisable WordPress site needs no
  flag; the Safety Snapshot covers the overwrite. An empty Site Root needs
  none either;
- **database** — the live database (or the relocated load database) holds
  tables, views, stored routines or events, **and none of its tables carries
  the site's table prefix** (owner decision 57): it does not look like this
  WordPress site, so it may be another application's. A WordPress database
  (a table with the prefix) or an empty one needs no flag; the Safety Snapshot
  and the automatic rollback cover the overwrite. The live database is always
  surveyed in a Mode that restores the database, as the WordPress account or,
  when that account cannot see it, the administrator; "not looked at" is
  never taken as empty.

A non-empty part is refused (exit 3 `REFUSED`) unless a flag waives it. The
message names the Mode as you typed it, every non-empty part with what was
found, and the exact flags that would authorize it, for example:

```
refusing to restore with --mode files (Restore Mode files) over a non-empty
target (files): found '/srv/www' (3 entries, none of wp-config.php,
wp-includes, wp-content); nothing was changed. --mode files --force would
authorize it
```

`--force` waives the parts the Mode restores; `--cleanfirst` chooses the clean
Modes. Waiving the files never waives the
database. There is no prompt. A Dry Run refuses the same way, with the same
exit code. The log record is `restore_refused` (`reason`, `parts`, `found`,
`detail`, `authorizing`, `restore_mode`); when the guard passes, the Run logs
`target_checked` with what it found and the waivers applied.

A Site Root that exists but cannot be read is refused (exit 3), never read as
empty; a missing one is refused as a usage error (exit 2).

Before a clean Mode empties the Site Root, `CLEAN` refuses (exit 3) a Site Root
that is the filesystem root, your home directory, or a directory holding
neither `wp-config.php` nor `wp-includes/`. This is a guard against a mistyped
`--wp-dir`, not a proof that the directory is a WordPress Site: a parent
directory that happens to hold either marker passes it. The control is the
explicit `--cleanfirst` (and `--force` over a non-empty target); check
`--wp-dir` before giving them (RT-006, ADR-0022 amendment 2026-11-07).

## A target with no `wp-config.php`

The Site Descriptor then comes from the Archive's config member, read into the
Run workspace (mode `0600`). Because an Archive can be forged, the database it
names is used only when you confirm it **or the Archive is encrypted**. An
encrypted Archive is authenticated by its passphrase before its config is
read, so its database is used as it says, logged (`archive_database_trusted`)
and shown as the first line of a Dry Run (owner decision 65). For an
unencrypted Archive, `--archive-database USER@HOST[:PORT]/NAME` must name the
same user, host (case-insensitive), port and database as the Archive's
config, or the restore is refused (exit 3) before any database connection. Name the database from your own records, not from
the Archive. When the flag is absent, the refusal deliberately does not show
the Archive's database, so it cannot be pasted back without checking it
(RT-S02-003). When yours differs, the refusal shows the Archive's user, host,
port and database, never a password, and names which of them differ; treat a
difference you did not expect as a sign the Archive may not be what it claims.
The refusal never echoes what you typed. A user part holding `:` (as in `USER:PASSWORD@…`)
is refused as a usage error (exit 2) without being shown. With a target `wp-config.php` the flag is not
needed and is ignored (logged `archive_database_unused`).

The WordPress database account and its password then also come from the
Archive. When that account cannot log in and an administrator is configured,
the administrator surveys the server; if it finds the account already exists,
the restore is refused (exit 3, `ExistingDatabaseUserError`) before anything is
changed, because restoring would reset that account's password to the
Archive's, and it may belong to another application. Pass
`--reset-existing-db-user` when the account belongs to this site. A missing
account is created as before, as `USER@'%'`, so WordPress can reach it from
another host, such as a separate web server or container (RT-S02-004).

A database-only restore (`--mode db`) onto such a folder loads the database
and **writes no `wp-config.php`** (owner decision 74): the files are not
restored, so neither is their config. The result says so
(`wp_config_absent: true`, and the line "wp-config.php: none at the target,
and none written"), and the Run logs INFO `wp_config_not_written` naming the
path. A relocation (`--new-db-*`) needs a `wp-config.php` to write its keys
into, so with none it is still refused (`NoConfigForCutoverError`).

## What the Dump can reach

The Dump is loaded into the live database as the site's WordPress database
account, with whatever rights that account already holds. A normal
Dump touches only the database it is loaded into, but a tampered one can name
other databases, and the server allows whatever the account is allowed. The
live database of the site being restored is covered by the Safety Snapshot;
**other databases the account can reach are not**. Give each site's account
rights on its own database only, and restore only Archives from a source you
trust, or encrypted ones, whose payload `age` authenticates (ADR-0022
amendment, 2026-11-06).

Such an account (the official `mysql` and `mariadb` images create
`MYSQL_USER` with rights on `MYSQL_DATABASE` only) is all a restore needs:
it loads its own database, and **no administrator is needed** (owner decision
55). An administrator (`--admin-username`/`--admin-password`, the
`L3IO_WP_BACKUP_ADMIN_USER`/`_PASSWORD` pair, or an AWS secret) is used only
when the database does not exist yet, the account does not exist or cannot
log in, or it lacks a right the load needs; then it creates the database or
the account and grants the account `ALL` on the database, and nothing else.
Without one, such a restore is refused at `PREFLIGHT`, before anything
changes (exit 4, `AdminCredentialsRequiredError`), naming the database, the
account and what is missing (`database_absent`, `missing_privilege`,
`authentication_failed`, `database_not_visible`). A load check that refuses
for any other reason names the database and the account it ran as
(`DatabaseRefusedError`, its `database` and `account`), never a password.

Started as root (owner decisions 58 and 71), a restore resolves a configured
administrator's credentials before it switches to the Site Root's owner, so
an AWS profile in root's home is read; the credentials are still used only
where an administrator is needed.

## Relocation: a new URL, database host, name or account

A restore can stand a Site up somewhere other than where it was backed up from,
for example staging from a production Archive:

```
wpbackup2 restore --wp-dir /srv/staging --archive site.tar.gz --mode all \
  --new-db-host db.staging:3307 --new-db-name staging_wp \
  --new-db-user staging_wp --new-db-password-file ~/.staging-db-password \
  --new-site-url https://staging.example.com \
  --new-site-home-url https://staging.example.com
```

Each flag is optional; a constant whose flag is absent keeps its value. Which
phase writes which `wp-config.php` constant:

| Constant | Flag | Written by |
|---|---|---|
| `WP_SITEURL` | `--new-site-url` | `REWRITE_CONFIG` |
| `WP_HOME` | `--new-site-home-url` | `REWRITE_CONFIG` |
| `DB_HOST` | `--new-db-host` | the cutover at the end of `LOAD_DUMP` |
| `DB_NAME` | `--new-db-name` | the cutover at the end of `LOAD_DUMP` |
| `DB_USER` | `--new-db-user` | the cutover at the end of `LOAD_DUMP` |
| `DB_PASSWORD` | `--new-db-password` | the cutover at the end of `LOAD_DUMP` |

The cutover exists only for a relocation: a restore without `--new-db-*`
flags writes no `DB_*` constant. The `DB_*` constants a relocation moves are
written by the journaled cutover, only after the Dump has loaded and been
checked, so the Site never points at a half-loaded database. `REWRITE_CONFIG` writes only the two URL constants. Each
constant is written by its own atomic write, read back before it replaces the
file: a mismatch fails the Run (exit 1) and leaves `wp-config.php` as it was
before that constant. Constants written earlier in the same phase stay written,
and the journal names them; for `REWRITE_CONFIG` the undo text gives each URL
constant's prior value.

- **`--new-db-host`** is `host` or `host:port` (port 1 to 65535); a socket
  path is not accepted. With `--new-db-name` alone, the Site's own host is used; with
  `--new-db-host` alone, the Dump is loaded into the Site's database name on
  the new host.
- **`--new-db-name`** loads the Dump into that database, which must be
  missing or empty (owner decision 73). A missing one is provisioned (an
  administrator is then needed) and dropped again if the Run fails; `DB_NAME`
  ends as that name.
- **The live database is untouched** by a relocation that loads another
  database, and the Safety Snapshot does not cover the relocated one: that is
  why the relocated database must hold no table.
- **`--new-db-user` and `--new-db-password`** go together: one without the
  other exits 2 `USAGE`, since keeping half from the Archive would mix two
  accounts. The account is provisioned with rights on the load database, and
  **the Dump is loaded as that account**, with whatever rights it holds (see
  "What the Dump can reach"). Give the password with
  `--new-db-password-file PATH` (its first line; make the file readable only
  by you) or the `L3IO_WP_BACKUP_NEW_DB_PASSWORD` variable (read only with
  `--new-db-user`) rather than `--new-db-password`, whose value is visible in
  the process list and which logs WARNING `password_on_command_line`. The
  first given wins: the flag, then the file, then the variable. The password
  is redacted from every log record and message and is never journaled; one
  shorter than 4 characters, too short to redact, is accepted with WARNING
  `credential_below_redaction_floor` (owner decision 61).
- **`--new-site-url` and `--new-site-home-url`** must start with `http://` or
  `https://`, name a host, hold no whitespace, control character, quote or
  backslash, and not end with `/` (WordPress stores them without one). Anything
  else exits 2 `USAGE` naming the flag and the rule. They set the two
  constants, and the old URL is then rewritten inside the database's content
  (below).

**Modes.** Every `--new-*` flag needs a Mode that restores the database
(`database`, `database-clean`, `all`, `all-clean`). With `files` or
`files-clean` any of them exits 2 `USAGE` before anything is changed: such a
Mode has no cutover, so the value would be accepted and silently discarded.

**A load database holding tables** (the `--new-db-name` database, or the
Site's name on the `--new-db-host` server) is refused before anything is
changed, naming it: exit 3 `REFUSED`, `RelocationTargetNotEmptyError` (owner
decision 73). `--force` does not waive this, whatever the tables are: nothing
is overwritten without a Safety Snapshot. Name a missing or empty database,
or empty it yourself after taking your own backup of it.

**An environment-driven `DB_*` is not rewritten.** A relocation flag whose
constant the target reads from the environment (`getenv_docker()`/`getenv()`)
is refused (exit 3, `EnvironmentDrivenTargetError`, naming the constants and
their variables) before anything is changed: change the container's
environment instead.

The Run logs `relocation_planned` once, with the constants each phase writes,
the relocated host, port and database name, and whether each URL changes; never
a URL value or the password. A Dry Run prints the same plan, naming the keys.

### Content URLs: WP-CLI `search-replace`

With `--new-site-url` or `--new-site-home-url`, the last phase,
`SEARCH_REPLACE`, rewrites the old URL across the restored database's content,
so widgets, menus and theme options keep working. It runs after
`REWRITE_CONFIG` and after `LOAD_DUMP` (and a relocation's cutover), because WP-CLI bootstraps
from the `wp-config.php` just written. It needs WP-CLI (`wp`) on `PATH`.

`PREFLIGHT` checks that `wp` is at least 2.12.0, and the printed plan's
preflight line names it (`wp >= 2.12.0`). Before any effect the restore also
prepares the owner-only state directory `wp` will run from and refuses (exit 3)
if a `wp-cli.yml` sits in it or above it.

- **What is replaced.** The old values are the restored database's stored
  `siteurl` and `home` options, read with `wp option list` (not `option get`,
  which would return the constants just written). Each is replaced by its new
  URL; when the two stored values are equal, one replacement is made, to the
  home URL if given (with only one of the two flags, that one replacement
  moves both, and the Run logs WARNING `search_replace_home_moved`). A stored
  value is the Archive's own text, so it must pass the same string rules as a
  `--new-site-url` (an `http://` or `https://` URL, no leading `-`, no
  whitespace, control character or quote; a trailing `/` is allowed): one that
  does not exits 3 `REFUSED` before anything is replaced, without printing it.
- **Order, and when it is refused.** The replacements run one after another,
  longer old URLs first. If one replacement's old URL occurs inside the other's
  new URL (for example `https://example.com/wp` and `https://example.com`
  moved to `https://example.com.au/wp` and `https://example.com.au`), the
  second pass would rewrite the first one's result. The order is changed when
  that avoids it; when no order does, the Run is refused (exit 3) before
  anything is replaced. Rewrite such pairs yourself with `wp search-replace`,
  through a temporary placeholder string.
- **How.** `wp search-replace <old> <new> --precise --all-tables-with-prefix
  --skip-columns=guid --report-changed-only`: every table with the Site's table
  prefix, plugin tables included. WP-CLI unserializes PHP-serialized values,
  replaces inside them and re-serializes them with corrected lengths; this tool
  never edits a serialized value or runs an SQL `REPLACE`. `guid` columns are
  left alone, as WordPress requires. Each replacement is previewed with
  `--dry-run` first, and re-counted afterwards: any old URL left fails the Run
  (exit 1). A replacement whose new URL contains its old URL (a move into a
  subdirectory of the same host) cannot be re-counted that way; it is skipped,
  with WARNING `post_check_skipped`.
- **What is not replaced.** Only the stored value, verbatim. JSON-escaped
  (`https:\/\/old…`), URL-encoded and protocol-relative (`//old…`) forms, a
  bare host name and the other scheme are left as they are; run
  `wp search-replace` for those yourself if the Site holds them.
- **Reported.** The restore report prints
  `content URLs: <n> replacements in <k> tables (<old> → <new>)`, and the JSON
  report's `search_replace` holds `replacements`, `tables`, `pairs`,
  `remaining` and `merged`. The Run logs `search_replace_completed` with the
  same and the WP-CLI version. A Dry Run prints the planned pairs (the old URL
  is read only after the Dump is loaded, so it shows as `<unresolved>`) and
  says the counts are determined after `LOAD_DUMP`; it changes nothing.
- **A failure rolls the relocation back** (owner decision 72). When the
  rewrite fails, before the Run exits the database is put back as a failed
  load's is (the Safety Snapshot reloaded into the live database, or, for a
  `--new-db-name` database, that database dropped again when this Run made it
  and emptied again otherwise) and the original `wp-config.php` is copied
  back from the copy saved before `REWRITE_CONFIG` wrote it (beside the Safety
  Snapshots, `<stamp>-<run id>-wp-config.php`), undoing the URL constants and
  any cutover at once. The Run logs WARNING `relocation_rolled_back` and exits
  with the rewrite's own error; the site is as it was before the restore,
  except files a file Mode extracted, so `--mode db` leaves no interrupted Run
  and `all` does. A rollback that fails is `RollbackFailedError` (exit 1),
  as for a failed load: the Run stays interrupted.
- **Interrupted.** A SIGTERM or Ctrl-C during the rewrite is not rolled back,
  and a failure after the first replacement leaves earlier ones made. The
  journal names `SEARCH_REPLACE` and the `wp search-replace` commands still
  owed. Check each first with `--dry-run`
  added, and re-run only those that still find their old URL. Never re-run
  one whose new URL contains its own old URL or the other command's old URL:
  it would rewrite its own results. When the stored `siteurl` and `home` were
  equal, only one of the two listed commands is owed.

**What runs inside WP-CLI.** WP-CLI loads WordPress to reach the database, so
code from the restored Site runs: `wp-config.php` itself, the mu-plugins in
`wp-content/mu-plugins` (WP-CLI loads them even with `--skip-plugins`), and
drop-ins such as `wp-content/db.php`. Regular plugins and themes do not run
(`--skip-plugins --skip-themes`), nor do globally installed WP-CLI packages
(`--skip-packages`).

- **No WP-CLI config is read.** `wp` runs from a fresh owner-only directory
  under this tool's per-user state directory, never from the Site Root or
  `$TMPDIR`. WP-CLI reads `wp-cli.yml` and `wp-cli.local.yml` from its working
  directory and every directory above it, and such a file can load PHP; if one
  is found there, the Run is refused (exit 3) before `wp` runs.
  `WP_CLI_CONFIG_PATH` names an empty file, so no `~/.wp-cli/config.yml` is
  read either. The `wp cli version` probe that preflight runs starts from the
  filesystem root, not from the Run workspace under the shared `$TMPDIR`, where
  another user could plant a `wp-cli.yml`; a `wp-cli.yml` there (the path as
  given and resolved) refuses the Run (exit 3) before the probe runs, and
  the probe also gets `--skip-plugins --skip-themes --skip-packages
  --no-color`.
- **The environment it gets.** Exactly `PATH`, `HOME`, `LC_ALL=C` and
  `WP_CLI_CONFIG_PATH`. Nothing else is passed on: not the Archive passphrase
  (`L3IO_WP_BACKUP_PASSPHRASE`), not any other `L3IO_WP_BACKUP_*`, `MYSQL_*` or
  `WP_CLI_*` variable. WP-CLI reads the database password from
  `wp-config.php`; none is passed by argument or environment. The same holds
  for the version probes run before anything is changed. This keeps the
  variables from being *inherited*; it does not hide them from the restored
  code, which runs as your user and could read this process's own
  `/proc/<pid>/environ` and `/proc/<pid>/cmdline`. On Linux, the process
  therefore marks itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`) while a
  `wp` child runs, which makes its `environ` unreadable to that child; as
  root, and on other systems, this does nothing, and `cmdline` (which holds
  `--admin-password` and `--new-db-password` when you pass them) stays world
  readable. Child processes
  started by the database library (the database client tools and `age`)
  still inherit this tool's environment; they run no Site code.
- **A time limit.** A `wp` call is killed after four hours and the Run fails
  (exit 1) with the step named, so restored code that never returns does not
  hold the Site Lock for ever. Its output is capped at 8 MiB on each of
  stdout and stderr; past that its whole process group is killed and the Run
  fails (exit 1).
- **Not as root.** A restore started as root runs as the Site Root's owner
  (below), so `wp` does too. A library caller running as root is refused; if
  WP-CLI is somehow reached as root, it is given `--allow-root` and the Run
  logs WARNING `wp_cli_running_as_root`.

**It is not confined (accepted for v1, owner decision 2026-11-07,
BL-E008-014).** The restored Site's `wp-config.php`, mu-plugins and drop-ins
run in the `wp` child **as the OS user running the restore**, with that user's
real `HOME` and every file it can read or write; no sandbox, namespace or
seccomp filter is applied. The limits above (environment, working directory,
time, output) narrow what the child inherits, not what it may do. Only a URL
relocation (`--new-site-url`, `--new-site-home-url`) runs `wp`; a restore
without one runs no Site code. What makes the code trustworthy is the Archive:
an **encrypted** Archive is authenticated by `age` before anything is changed,
so the code that runs is the code that was backed up with your passphrase. An
**unencrypted** Archive is not authenticated, and whoever could write it could
have put PHP in it. Such a restore logs the WARNING `archive_unauthenticated`
(and, under `--log-format console`, prints it) before any effect, URL
relocation included; restore an unencrypted Archive with a relocation only
from a source you trust, or pass `--require-encrypted`. Optional confinement of
the `wp` child is planned after v1.

## TLS to the database

By default no TLS is required, for backup or restore, the WordPress account
or the administrator: the tool connects the way WordPress does (owner
decision 56, amending BL-E002-114's verified-TLS policy). `--db-tls
{required,verify-ca,verify-identity}` (also `L3IO_WP_BACKUP_DB_TLS`, config
key `db_tls`) requires it on every connection of the Run, local hosts
included; `--db-tls-ca FILE` names the CA bundle for a private CA, with
`verify-ca` or `verify-identity` (alone, or with `required`, it exits 2
`USAGE` before any connection). A server that cannot complete the required
handshake exits 5 `UNREACHABLE`; one whose certificate does not verify exits
3 `REFUSED`. `--admin-tls` and `--admin-tls-ca` are deprecated spellings of
the two, logged as WARNING `deprecated_option`.

On every connection, a server that asks for an authentication plugin that
would expose the password (`mysql_clear_password`, `mysql_old_password` or
`dialog`) is refused before any password is sent: exit 3 `REFUSED`, naming
the plugin and the server (RT-S02-003; `l3io-wordpress-database` 0.3.2).

## Interrupted Runs

The Run Journal and the Site Lock live in the per-user state directory of the
OS user running the restore. A restore of the same Site by another OS user
neither sees that journal nor waits for that lock, so run every restore of a
Site as the same user.

The lock is keyed by the Site Root's resolved path (symlinks followed), as
spelled. Two spellings of one directory that resolve differently are two locks
(an accepted limit, BL-E002-083): on a case-insensitive filesystem (macOS by
default) `/srv/Site` and `/srv/site`, and on Linux a bind mount of the Site
Root at a second path. Always name a Site by the same path.

A Run killed part-way leaves its journal open. The next restore of the Site is
refused (exit 3), naming the interrupted Run, its phase, how to undo it and the
journal's path. `--resolve-interrupted-run RUN_ID` acknowledges it (completing
a relocation's interrupted `wp-config.php` cutover first) and stops: resolving
and restoring are never one command. A Run that stopped while the live
database was partly loaded (a kill during the load, or a failed automatic
rollback) is only acknowledged: resolution logs WARNING
`database_rollback_owed` with the Safety Snapshot and the command that
restores it (`wpbackup2 restore --wp-dir <root> --archive <snapshot> --mode
db`), which you then run. A different ID is refused, naming the
journal's. `--archive` is still required alongside it, even though resolving
reads nothing from the Archive when `wp-config.php` is present.

The Run Journal's file format is unchanged by the direct load: a journal an
earlier version left (with its staging steps) is still read, and its undo
text is the one that version gave.

Where no Safety Snapshot was taken, the undo text says why, as the journal
records it (`snapshot_skipped`, from 1.1.0): `no Safety Snapshot: not needed:
the target was empty`, or `no Safety Snapshot: opted out
(--no-safety-snapshot)`. A journal an earlier version wrote records no reason,
and says `no Safety Snapshot: none was taken`.

Before completing a cutover, resolution surveys the database the interrupted
Run was switching to. If it no longer exists or is not a WordPress database,
nothing is written and the resolution is refused (exit 3 `REFUSED`), naming
it: set the keys already written back to the values the journal records, or
restore from the Safety Snapshot, then resolve again. When `wp-config.php`
already holds its pre-Run values (no key landed, or you set them back), there
is nothing to complete: resolution writes nothing, logs the WARNING
`cutover_abandoned`, and acknowledges the Run.

A journal that cannot be read back (`CorruptJournalError`, exit 3) names no
Run to resolve, so `--resolve-interrupted-run` cannot clear it. Inspect the
Site and the journal file the refusal names, repair the Site if needed, then
move that file aside (for example rename it with a `.corrupt` suffix) before
restoring again.

## How a Run is driven

`drive_restore` (in `cli/restore.py`) is the one driver both the CLI and the
library call. Everything before the executor only reads; the executor
(`execute_restore`, in `adapters/restore_runner.py`) runs the plan's phases in
order, and the live database changes only in `LOAD_DUMP`, after the Safety
Snapshot, and is rolled back from it on a failure.

```mermaid
flowchart TD
    A[Site Lock and the Run: Run ID, workspace, registry] --> B[Run Journal query]
    B --> C[Site Descriptor, credential sources, the one adapter bind]
    C --> D{--resolve-interrupted-run?}
    D -- yes --> D1[resolve the interrupted Run, complete its cutover if needed] --> Z[stop: nothing restored]
    D -- no --> E{journal from an incomplete Run?}
    E -- yes --> E1[refuse: exit 3, naming the Run and its phase]
    E -- no --> F[probe the Archive]
    F --> G[build the plan: Mode, FR-7 non-empty guard]
    G --> H[journal header and inputs]
    H --> X
    subgraph X [execute_restore]
        P[PREFLIGHT: tools, passphrase backend, tarfile fix, size and free space, database checks] --> V[VERIFY: checksums, decrypt the payload]
        V --> S[SNAPSHOT: Safety Snapshot, unless skipped]
        S --> CL[CLEAN: files-clean and all-clean only]
        CL --> EX[EXTRACT]
        EX --> RC[REWRITE_CONFIG: relocation constants other than DB_*]
        RC --> L[LOAD_DUMP: drop the prefixed tables, load into the live database, check it; roll back from the snapshot on failure; a relocation's DB_* cutover]
        L --> SR[SEARCH_REPLACE: relocation only]
    end
    X --> O[outcome and report]
```

Every phase is journaled before it starts and after it completes. Only the
phases the plan holds run: a `files` Mode has no `LOAD_DUMP`, a `database`
Mode no `EXTRACT`, and `--no-safety-snapshot` removes `SNAPSHOT`.

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

**What authentication proves, and what it does not.** For an encrypted
Archive, `wpbackup2 verify --authenticate` and the restore's `VERIFY` decrypt
the whole payload with the passphrase; `age` authenticates every chunk. The
payload ends with an inner copy of the Manifest, and both then require it to
match the unencrypted `manifest.json` field by field, less the payload's own
size and checksum. A Manifest edited outside the payload (its scope, source or
member list), or a payload swapped in from another Archive, is refused with
`PayloadDamagedError` (exit 8), naming the first field that differs, before the
Safety Snapshot and before anything destructive. **A rollback is not
detected:** a whole older Archive encrypted with the same passphrase, put in
place of a newer one, is genuine, so it passes. Detecting that needs a record
kept outside the Archive, such as the name or checksum of the latest backup,
which this package does not keep.

**The scrypt work factor, and Archives written by 1.1.1 and earlier.** The
`l3io-age-passphrase` binder up to 0.1.2 let `age` pick the passphrase's
scrypt work factor by timing a small scrypt run, and refused any file whose
work factor was more than 4 above what that timing suggested. On a heavily
loaded host the timing ran long, so `verify --authenticate` or a restore's
`VERIFY` could refuse an intact Archive as `PayloadDamagedError` (exit 8,
ERROR `member_decryption_failed` with `reason` `corrupt`). From 1.1.2
(`l3io-age-passphrase` 0.1.3) the work factor is fixed: `wpbackup2` encrypts
at 18, as stock `age -p` does, and decrypts anything up to 22 however busy the
host, so that false refusal is gone. A payload above 22, which
1.1.2 never writes, is refused as `WorkFactorTooHighError` (exit 3, `reason`
`work_factor_too_high`), not reported as damaged; stock `age` refuses it too.

The other side of the old defect remains in Archives already written: one
*written* by 1.1.1 or earlier on a busy host may have a low work factor, so
its passphrase is cheaper to guess. Re-take any backup that matters with
1.1.2 or later. The work factor is the last field of the payload's `age`
header line `-> scrypt <salt> <log_n>`, readable without the passphrase, for
example with
`tar -xzOf <archive> payload.tar.gz.age | head -c 200 | grep -a '^-> scrypt'`;
anything well below 18 is worth replacing.

**Encryption protects file contents only.** `manifest.json` is never
encrypted: the Site Root path, the database name, every file path and size and
the exclusions stay readable in clear in an encrypted Archive, to anyone who
can read it, without the passphrase.

**Downgrade by substitution: `--require-encrypted`.** Authentication binds only
Archives that are encrypted. Someone who can write the Storage Target can
replace an encrypted Archive with a forged plain one, and a restore that
finds no encryption does not know one was expected. By default it restores the
plain Archive with the warning above (plain Archives are not authenticated,
ADR-0016 amendment). That holds when a passphrase was given too: the restore
proceeds, with WARNING `archive_not_encrypted` on stderr, since a passphrase
meant an encrypted Archive was expected (owner decision 59, reversing the
2026-11-07 refusal; `--allow-unencrypted` is deprecated and does nothing).
`restore --require-encrypted` refuses any Archive that is
not encrypted, before anything changes, with `ArchiveNotEncryptedError` (a usage
error, exit 2, the same refusal `verify --authenticate` gives a plain Archive).
Use it in cron and disaster-recovery runs that only ever restore encrypted
Archives.

**A target the Run cannot write is refused.** The target survey, before any
effect and in a Dry Run too, checks that the user the Run runs as (after a
root start's switch, the Site Root's owner) can write everything the Mode
would write: the Site Root itself, every existing directory the Archive's files
go into, every existing file they replace, every directory `--cleanfirst`
clears, and where a laid-down `wp-config.php` is created. One it cannot is
refused, exit 3 `REFUSED`, `TargetNotWritableError`, naming the paths and the
uid; `--force` does not waive it, and nothing is begun, so the next Run needs
no `--resolve-interrupted-run`. (Before 1.1.0 such a restore passed
`PREFLIGHT` and `VERIFY`, failed at `EXTRACT` with "could not write archive",
and left an interrupted Run.) The usual cause is restoring as a user other
than the Site Root's owner: see [Running as root](#running-as-root). The
refusal says how to fix it and that wpbackup2 never changes ownership (owner
decision 87): ownership is the operator's. It needs a user that can read the
site's `wp-config.php` and the Archive: a user that cannot read the config (a
`0640` file of another user's, say) is refused earlier, exit 4
`CREDENTIALS`, since the config is read first. The Archive, the state
directory, `TMPDIR` and `--snapshot-dir` are checked before the Run opens
too: exit 3, `PathNotAccessibleError`, naming the path, the uid and gid, the
owner, group and mode found, and the fix. A write
that still fails during `EXTRACT` (a file changed after the check) names what
it was writing into, `could not write into the Site Root '<path>'`
(`ExtractionWriteError`, exit 1).

**Quiesce the Site first.** A restore does not stop the web server, put the
Site into maintenance or lock its database. Whatever is written to the Site
Root or the live database after the Safety Snapshot is taken and before the
restore finishes is in neither the snapshot nor the Archive, and `CLEAN` or a
Mode that drops the live database removes it. The re-survey before the first
destructive step refuses only a part that went from empty to holding content,
not a change to a part that was already non-empty (an accepted limit,
BL-E003-113: nothing short of stopping the Site's writers can detect it). Take
the Site offline (or stop its writers) before running `restore`.

1. **Preflight** — the required tools, the passphrase backend when the Archive
   is encrypted, the interpreter's tarfile fixes, free space, and (for a Mode
   that loads the Dump) the database checks.
2. **Verify** — the Archive is verified. A corrupt Archive exits
   `VERIFICATION_FAILED` (8). An encrypted Archive's payload is then decrypted
   into the Run workspace; a wrong passphrase or a damaged payload also exits
   `VERIFICATION_FAILED`. A v0 Archive (no Manifest) is reported as
   `STRUCTURAL_ONLY`, never as `VERIFIED`. A v0 Archive holding a hard link
   (0.2.21 wrote one for any Site file with more than one link) fails
   verification with `UNSUPPORTED_MEMBER_TYPE` and cannot be restored; take a
   fresh backup of that Site with this version instead. An Archive that changed
   between planning and verification is refused with the same exit code.

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

Both refusals exit `REFUSED` and change nothing. A space refusal names the
filesystem that is short and the paths on it, the space needed, free and
short (in MiB, with the exact bytes), and the remedy: free that much, or use a
location on another filesystem (the Run workspace follows `TMPDIR`, the Safety
Snapshot `--snapshot-dir`). The expansion limit has no override. For a v1 Archive the sizes come from its Manifest;
for a v0 Archive they are read from its tar headers. During extraction a member
whose header states more bytes than its Manifest entry is refused. Every
refusal at a member says the Archive may be damaged or tampered with and to
check it with `wpbackup2 verify --archive <archive>`.

**These two checks are advisory.** They run in preflight, before the Archive
is verified, so they trust the sizes its index states (ADV-12, SD-01-4). A
forged index can understate its sizes and pass them; a corrupt Archive can be
refused here (exit 3) before verification would have called it corrupt (exit
8). Verification and extraction are the enforcement: verification checks every
member against the Manifest, and extraction holds each member to its indexed
size with a running total, so an Archive never writes more than its index
declares.

When the Run takes a Safety Snapshot of the files, the Site Root's current file
bytes are charged to the snapshot directory's filesystem. Destinations on the
same filesystem (the target, the workspace and the snapshot directory) are
summed against its one free space.

Not checked in advance (accepted limits, owner decision 2026-11-07):

- **The database part of a Safety Snapshot** is charged 0 bytes: its Dump size
  is not known before the dump, and no live-database size is queried
  (BL-E003-048). A snapshot that runs out of space fails while it is written,
  before anything is destroyed, and the restore stops with `REFUSED`.
- **The database server's disk** is not checked at all (BL-E003-116). The Dump
  is loaded into the live database after its prefixed tables are dropped, so
  the server needs room for the restored database; this tool cannot see that
  disk. A load that fills it fails in `LOAD_DUMP`, and the automatic rollback
  reloads the Safety Snapshot. Check the server's free space first for a
  large Site.

**Memory when reading an Archive (owner decision 2026-11-07, BL-E008-011).** The
Manifest is read whole and parsed before anything is authenticated, taking
about 4.3 times its size in memory. The Manifest cap is 256 MiB, so the worst
case is about 1.1 GB; an ordinary Site's Manifest is a few MB. `verify` reads
it the same way. A streaming parse, then an on-disk member index for truly
bounded memory, are planned after v1.

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

The passphrase comes from `L3IO_WP_BACKUP_PASSPHRASE` or, on a terminal, a
prompt that runs before the Archive is read. The one passphrase decrypts an
encrypted Archive and encrypts the snapshot, so the prompt asks for it on every
interactive restore and says both uses; leave it empty for none. With a
passphrase, stderr says `safety snapshot is encrypted with the passphrase this
restore was given: <path>`, and restoring that snapshot needs it. Without one
the snapshot is written unencrypted and the Run says so on stderr (`safety
snapshot is NOT encrypted: <path>`). When a later phase fails, the snapshot's
path is printed with the failure.

A restore takes a passphrase of any length, so an Archive encrypted before the
12-character floor existed stays restorable; one shorter than 12 logs WARNING
`passphrase_below_encryption_floor` (it also encrypts the snapshot), and one
shorter than 4 also logs WARNING `credential_below_redaction_floor`, naming
where it came from, never the value: it is too short to be masked in logs
(owner decision 69). `verify --authenticate` accepts it the same way.

The published snapshot file is mode `0600`, and every directory the Run
creates for it is `0700` (an existing directory is left as it is); a published
file readable by anyone else stops the Run with `REFUSED` before anything is
destroyed. An unencrypted
snapshot holds the Site's `wp-config.php` (with its database password) and a
Dump of its database. **Nothing removes snapshots, or the `wp-config.php`
copies `--overwrite-wp-config` saves beside them:** each stays in its directory
until you delete it, so delete those you no longer need (accepted for v1,
BL-E003-087; a snapshot `prune` command is planned after v1).

**A `--snapshot-dir` below a one-level-up config's directory is refused.** When
the Site keeps its `wp-config.php` one level up (`~/public_html` with
`~/wp-config.php`), every directory below that config's directory counts as
possibly web-served, so a snapshot directory anywhere under `~` is refused,
even one no web server serves (BL-E003-033, accepted). The default, the
per-user state directory, is also under `~` in that layout; pass
`--snapshot-dir` naming a directory outside it, such as one under `/var/backups`.

## `wp-config.php`

By default the target's `wp-config.php` is **kept**: the Archive's copy is
never extracted over it, `--cleanfirst` does not remove it, and only a
relocation's cutover (`--new-db-*`) changes it. The Run logs `wp_config_kept`. When the target has
no `wp-config.php`, the Archive's copy arrives with the files.

`--overwrite-wp-config` (file Modes only; with `--mode db` it exits 2
`USAGE`) replaces the target's file with the
Archive's, keeping the target's own `DB_HOST`, `DB_USER`, `DB_PASSWORD` and
`DB_NAME`, so the Site never points at the source's database. Each of the four
is carried into the Archive's file **as the target wrote it**, not as a value:
`define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );` stays a
lookup that follows the container's environment, and a literal stays that
literal (owner decision 48). A literal lands exactly as before unless the
target spelled it differently from `'value'` (double quotes, say): the
target's spelling is kept, with the same PHP value. A copy of the
original is saved first (mode `0600`) beside the Run Journal, and the Run logs
the WARNING `wp_config_overwritten`. An Archive without a `wp-config.php` is
refused with this flag (exit 3; omit it to keep the target's), as is one whose
`wp-config.php` is larger than 1 MiB, before anything is changed. The
Archive's copy is read into memory and written only once, over the target's
file; no other copy of it is written, and a Dry Run writes none.

**The Archive's file is applied whole, code included.** Apart from the four
`DB_*` constants, nothing in the Archive's `wp-config.php` is filtered or
inspected: any PHP it holds beyond its settings (an `include`, a function
call, an edit an attacker made) runs on the target, exactly like the plugins,
themes and core files the same Archive restores. The Run gives no separate
warning about it. Restore only Archives you trust, and keep the default
(the target's `wp-config.php`) when you are unsure of the Archive's copy.

**A target whose `DB_*` are read from the environment.** The official
WordPress Docker image defines its settings as, for example,
`define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );`.
Every restore resolves such a definition from the restore's own environment,
as a backup does, so run it with the site's variables (a sidecar given the
WordPress container's environment file). A database restore then loads
straight into the database the environment names and writes no `DB_*`
(owner decisions 47 and 49): the site keeps following its environment. Only a
relocation flag that would rewrite such a constant (`--new-db-name` for an
environment-driven `DB_NAME`, say) is **refused** before anything is
changed, a Dry Run included, with exit 3 `REFUSED`
(`EnvironmentDrivenTargetError`, naming the constants and the variables they
read, never a value): change the container's environment instead.

With `--overwrite-wp-config`, a files-only restore carries those definitions
into the Archive's file as written (above). `getenv_docker()` is not a PHP
function: the image's `wp-config.php` defines it. So when the target reads
any of its `DB_*` through `getenv_docker()`, the Archive's file must define
it before those definitions, or PHP would stop every request with "Call to
undefined function getenv_docker()". An Archive's file that does not is
**refused** before anything is changed, a Dry Run included, with exit 3
`REFUSED` (`UndefinedConfigFunctionError`, naming the constants); keep the
target's `wp-config.php` (omit `--overwrite-wp-config`), or restore from an
Archive taken from a Docker site. `getenv('NAME')` needs nothing: PHP defines
it.

That check is `l3io-wordpress-config`'s own (`declares_function`, 0.1.7): a
lexical search of the Archive's file, outside comments and strings, for
`function getenv_docker(` or `function &getenv_docker(` (in any case) before
the first carried definition. Its limits are the library's, as it documents
them:

- **It can accept a file PHP would still reject:** a declaration in a branch
  PHP never runs, inside a class (a method), nested in another function, or
  inside a heredoc string, still counts. The restored site then fails with
  that error; the saved copy of the original (above) is the way back.
- **It can refuse a file PHP would run:** the function defined in a file the
  config includes (`include`/`require` is not followed), or declared after
  its use (the check is text order; PHP hoists a top-level function). Keep
  the target's file then.

How a database restore reaches such a site's data (loading into the database
the environment names, after the Safety Snapshot, with an automatic rollback)
is [ADR-0027](adr/0027-restore-into-an-environment-driven-database.md),
**Accepted** (owner decisions 49 to 55).

Any other PHP expression in `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` or
`$table_prefix` (anything but a literal, `getenv_docker('NAME', <literal>)` or
`getenv('NAME')`) is refused, exit 3 `REFUSED` (`UnsupportedConfigValueError`),
by every command that reads it: this tool does not evaluate PHP.

### Where the config is, and where it is laid down

WordPress reads `<Site Root>/wp-config.php`, else `<Site Root>/../wp-config.php`
when the parent directory holds no `wp-settings.php` (a parent that holds one
is another WordPress install). A restore finds the target's config the same
way, or uses `--wp-config PATH` as given. The INFO record `config_located`
names the file, its location (`wp-config.php` or `../wp-config.php`), and
whether it was found on the target, recorded in the Manifest or defaulted.

A Site that keeps its config one level up is backed up with that file as the
Archive member `config/wp-config.php` (never a `../` path), and the Manifest
records `config_path: "../wp-config.php"`. When the target has **no** config,
a Mode that restores files lays the Archive's copy down where the Manifest
says, before any constant is rewritten: restoring into `<dir>/public` writes
`<dir>/wp-config.php` and nothing at `<dir>/public/wp-config.php`. The journal
names that file, so `--resolve-interrupted-run` completes a relocation's
interrupted cutover in it. A Dry Run prints `lay down <path>` and writes nothing.

A target config WordPress reads is otherwise used as it is. Refused before
anything is changed (exit 3):

- a Manifest `config_path` other than those two values, with or without
  `--wp-config`; it is never used as a path;
- when the Manifest records `../wp-config.php` and the target has no config
  WordPress reads: a parent directory that holds `wp-settings.php`;
- when the Manifest records `../wp-config.php`: a parent `wp-config.php` next
  to an existing `<Site Root>/wp-config.php` (WordPress reads the Site Root's,
  so remove the one you do not mean);
- anything that is not a regular file (a directory, a dangling symlink) where
  the config would be laid down.

A database-only Mode never lays a config down; with no target config it is
refused as described under [A target with no `wp-config.php`](#a-target-with-no-wp-configphp).
An Archive that records no location (a v0 Archive, or a v1 one made with a
`--wp-config` outside both places) has its `wp-config.php` laid down at the
default, `<Site Root>/wp-config.php`, by a Mode that restores files. With a URL relocation, a `--wp-config` that is not the file
WordPress loads for the Site (the Site Root's when both exist) is refused as a
usage error (exit 2), since WP-CLI would rewrite URLs from another config.

The Site Lock covers the Site Root, not its parent: a `wp-config.php` created
one level up after the checks and before the lay-down is not detected, and the
lay-down overwrites it; it is in neither the pre-restore checks nor the Safety
Snapshot.

The laid-down file's mode is logged (INFO `config_laid_down`). It holds
`DB_PASSWORD`, so every "other" permission bit is taken off it straight after
it is written, whatever the Archive's mode was, with WARNING
`config_mode_restricted` (path, the Archive's mode and the new mode) when that
changed anything. Owner and group bits are kept (`0644` becomes `0640`, not
`0600`: PHP often reads the file through a shared group; owner decision 43).
When the config is laid down
by itself, the Archive's own `wp-config.php` in the Site Root tree is not
extracted, so it cannot shadow the one WordPress is meant to read.

## Symlinks

A symlink whose target is absolute or resolves outside the Site Root is **not
supported**. It is skipped on restore with a WARNING (`symlink_skipped`), and
the Run still exits `OK`. A Dry Run lists those links after its actions; for a
v0 Archive, which records no link targets, they are reported at extraction.

The Dry Run decides from the Archive alone, by the link's own text: a target
that starts with `/`, or that leaves the Site Root once joined to the link's
directory. Extraction decides on disk, following symlinks that already exist
on the target. The two agree for every link the Archive itself defines; they
can differ when a directory on the target is already a symlink (for example
`wp-content/uploads` pointing at `/mnt/media` in a `files` restore, which does
not clear the Site Root). Then the real Run may skip or refuse members the Dry
Run did not list. The real Run's WARNINGs are the authority.

## Dry Run

`wpbackup2 restore --whatIf ...` (or `wpbackup2 --whatIf restore ...`) prints
the ordered actions the real Run would take and runs every check the real Run
runs, so it refuses where the real Run would, with the same exit code. It
changes nothing: not the Site, the database, any Storage Target, the snapshot
directory or the journal directory. Only the Run workspace is written (an
encrypted payload is decrypted there to check the passphrase), and the Site
Lock is taken as for a real Run: its lock file is created under the per-user
state directory, and a Dry Run is refused with `LOCKED` (exit 9) while
another Run of the same Site holds it.

## What a Run prints

Results go to stdout; warnings and the log go to stderr.

With the default (console) log format, stdout carries the Dry Run's ordered
actions, then the links it would skip (`would skip symlink '<path>' ->
'<target>': <reason> (not supported)`, or for a v0 Archive one line saying they
are reported at extraction), the database report, the search-replace count, the Safety Snapshot's
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
 "snapshot_encrypted": false, "wp_config_kept": true,
 "wp_config_absent": false}}
```

(shown wrapped; it is one line). `status` is `completed`, `planned` (a Dry
Run, whose `actions` list the plan), `resolved` or `would_resolve`
(`--resolve-interrupted-run`), or `failed` (the database report does not call
the database restored). `skipped_links` lists each link extraction skipped
(`path`, `link_target` and `reason`: `absolute` or `outside_destination`); in a
Dry Run, the links the real Run would skip, and `null` for a v0 Archive, whose
links are reported only at extraction. `database_report` holds the same fields as the
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
was created, the bytes loaded and the previous database's fate, after the
verdict on the first line: `database restored`, or `database NOT restored:
<reason>` (owner decision 2026-11-07, BL-E003-106; the JSON form is
unchanged).

## Running as root

A restore started as root reads first what only root may read: the config
file, the passphrase, `--new-db-password-file`, the target's `wp-config.php`
(for `--archive latest`) and the Archive itself, which it keeps open. It then
switches the whole process to the uid and gid that own the Site Root
(`setgroups`, `setgid`, `setuid`, in that order, each read back; regaining
root must fail) and runs the Run as that user, logging INFO
`restore_runs_as_site_owner` (owner decision 58, reversing decision 1's
`--allow-root`, now removed). Restored files belong to the site's owner, and
the extraction race a root restore had (a symlink swapped in by a local user
between a member's check and its open) has no root to win. `HOME` becomes the
owner's home directory from the password database, so the per-user state
directory (the Site Lock, the Run Journal, the default Safety Snapshots) is
the owner's; an `XDG_STATE_HOME` you set is kept and must then belong to that
owner. After the switch the process is not dumpable, so the site's PHP,
running as the same user, cannot read its environment.

A backup started as root switches the same way (owner decision 67), after
reading the config file, the passphrase and the database credentials (the
whole chain, `wp-config.php` and an AWS secret included), and logs INFO
`backup_runs_as_site_owner`. A backup and a restore of one site therefore
share the owner's state directory and Site Lock, and the Archive is the
owner's file. The Archive directory (the `--archive` target's directory,
`--archive-dir`, or the default one, now the owner's) must be writable by the
owner: otherwise the backup is refused after the switch and before the Run
opens, a Dry Run too (exit 3, `ArchiveDirectoryNotWritableError`, naming the
directory). The Run reads `wp-config.php` again as the owner (its other
settings, and its copy in the Archive), and the database client opens
`--db-tls-ca` during the Run, so both must be readable by the owner, as the
site's own PHP must read `wp-config.php` anyway.

**Restoring an Archive another user wrote.** The disaster-recovery case: the
Archive was written by one uid (the old host's), and the new Site Root belongs
to another. Start the restore as root and it runs as the Site Root's owner,
so it can write the Site Root and the restored files are the site's:

```
sudo wpbackup2 restore --wp-dir /var/www/html --archive /backups/wpbak-example-20261111T030000Z.tar.gz
docker compose run --rm -e PUID=0 backup restore --archive latest   # the Docker sidecar (docs/docker.md)
```

The restored files are the site's because the Run runs as the Site Root's
owner; nothing's ownership is changed (owner decision 87). The state directory
must be that owner's too (in the Docker image, the state volume:
[docs/docker.md](docker.md#ownership)), otherwise the restore is refused
before it opens (exit 3, `PathNotAccessibleError`, with the fix).

Run as another non-root user that cannot write the Site Root, the restore is
refused before any effect: exit 3 (`TargetNotWritableError`, above) when that
user can read the site's `wp-config.php` and the Archive, exit 4
(`CREDENTIALS`) when it cannot read the config, which is read first.

Refused (exit 2 `USAGE`, before the Run opens): a Site Root owned by root,
which has no owner to switch to (`RootRestoreRefusedError`, or
`RootBackupRefusedError` for a backup and `RootPruneRefusedError`, a
`RootBackupRefusedError` in a prune's words, for a prune; before anything else
is read). A switch that does not hold stops the Run (exit 3,
`PrivilegeDropError`). The library (`api.restore`, `api.backup`, `api.prune`)
never changes its host's user, so it
refuses a Run as root (owner decision 68); drop privileges to the site's
owner first, or use the `wpbackup2` command. `verify` touches no Site, so it
neither switches nor refuses.

The installation must be readable by the owner too. Python imports a module
on first use, so after the switch the owner loads the rest of `wpbackup2`,
its dependencies and the standard library itself; an interpreter or virtual
environment under root's home directory (mode `0700`, as `uv tool install`
or `pipx install` run as root leave it) cannot be read. Right after the
switch, each directory of `sys.path` after the first (the standard library,
`site-packages`, an editable install's source tree) is checked, and one the
owner cannot read refuses the Run before it opens, a Dry Run too (exit 3,
`InstallationNotReadableError`, naming the directory). Install `wpbackup2`
where the site's user can read it (a system-wide or `/opt` install), or run
it as that user.

## `--archive latest`

`--archive latest` (also `L3IO_WP_BACKUP_ARCHIVE=latest`, config key
`archive = "latest"`) restores the newest Archive **of this site** in the
Archive directory: `--archive-dir`, else the current directory when it is
outside the Site Root, else the per-user data directory, the same directory a
backup writes to by default (owner decision 64). Only each Archive's
Manifest is read, not the rest of it.

"This site" is what the Manifest records of the Site the backup read: its
Site Root and its database host (any case), port and name, and its table
prefix, all five equal to the target's, as its `wp-config.php` gives them. A
directory shared by several sites therefore never yields another site's
Archive; two sites alike in all five cannot be told apart, so give each its
own `--archive-dir`. A target with no `wp-config.php` cannot be told apart
from any other and is refused (exit 2); name the Archive then. Safety
Snapshots, files that are no Archive and Manifests without a readable
`created_at` never count; a tie goes to the later file name. None found is
`NoArchiveForSiteError` (exit 2). The file name plays no part: an Archive
under the default name of 1.1.0 (`wpbak-<site>-<UTC stamp>.tar.gz`), the
earlier one (`wpbackup2-<db>-<stamp>.tar.gz`) or any name you gave it is
chosen by its Manifest alike. The choice is logged
(`archive_latest_selected`) and is the first line of a Dry Run. `--archive`
stays required: a restore never runs on an Archive nobody named.
