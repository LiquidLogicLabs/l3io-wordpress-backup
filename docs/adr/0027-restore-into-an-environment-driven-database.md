# ADR-0027: A database restore loads straight into the live database, after the Safety Snapshot, and rolls back automatically

- **Status:** Accepted
- **Date:** 2026-11-08 (proposed); 2026-11-09 (rewritten to the owner's decisions and accepted)
- **Deciders:** Shawn Anderson (owner decisions 49, 50, 51, 52, 53, 55; with 57 and 63 for the flags)
- **Epic:** close-out (owner decision 47, restore half; automation-first change set, step 4)
- **Resolves:** owner decision 47, "Restore, for an env-driven `DB_NAME`"; the sidecar test's phase C (2026-11-08); the friction of an administrator on every restore (decision 55)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-18--a-killed-run-must-be-recoverable
- **Also departs from:** ADR-0022's staging-database cutover (the live site is no longer intact while the Dump loads; the Safety Snapshot is the rollback) and AD-29 (amended: the WordPress account observes its own database)
- **Principle(s) in tension:** ADR-0022 (the live site stays intact if the load or its check fails), AD-28 (backup issues no SQL; database does), AD-4 (a Dry Run refuses where the real Run would), AD-18 (every effect is journaled and can be resolved), AD-29 (existence is the administrator's answer)

## Context

Until this ADR, a restore that loaded the database never loaded over the live
database. It loaded the Dump into a new staging database,
`<live name, first 51 characters>_l3s_<first 8 of the Run ID>`, checked it, and
switched the site to it by rewriting `wp-config.php`'s `DB_NAME` (the cutover,
ADR-0022). The live database was kept (`database`, `all`) or dropped
(`database-clean`, `all-clean`, with `--drop-database`). Every such restore
needed administrator credentials, to create the staging database and grant the
site's account on it.

That design fails the official `wordpress` Docker image, which defines its
settings from the container's environment:

```php
define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );
```

Rewriting that line replaces the lookup with a fixed string, and the site
stops following its environment. The tool cannot change the variable instead:
it belongs to another container's definition. Until now such a restore was
refused (`EnvironmentDrivenTargetError`).

It also fails the owner's goal for the release: `wpbackup2 restore --archive
latest` should run unattended, as a Docker sidecar or a simply installed Python
app, with no extra flags. An administrator on every restore, a new database
name after every restore, and a confirmation flag for the database name are
each a cost to justify (decision 51).

The proposal this ADR first carried (option A below: stage, then swap the
tables into the live name with one `RENAME TABLE`) was rejected by the owner
(decision 49).

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Load into a staging database, check it, then move its tables into the live name with one multi-table `RENAME TABLE` | The live site is intact until one atomic statement; undo is a rename back | New database API; administrator privileges on three databases; views, triggers, routines, events and foreign keys refused; a new resolution path | Keeps ADR-0022's guarantee |
| B. **Load directly into the live database after the Safety Snapshot; on failure reload the snapshot** | No new SQL beyond a prefix-scoped drop; the WordPress account suffices; works for every target, literal or environment-driven | The site serves a database being replaced for the length of the load; recovery from a failure is a second full load (the snapshot) | Relaxes ADR-0022's guarantee to "recovered automatically from the snapshot" |
| C. Rename the database | One name change | MySQL and MariaDB have no `RENAME DATABASE` | Not available |
| D. Leave the swap to the operator | No new SQL | The restore ends half done by design | Breaks FR-9's "restored" report |
| E. Rewrite `wp-config.php` anyway | Works today | Turns the lookup into a constant | Rejected by decision 47 |

## Decision

**B, for every database restore, literal and environment-driven `DB_*`
alike** (owner decisions 49 and 55). The staging database and the `DB_NAME`
cutover are removed.

**Sequence (`LOAD_DUMP`).** After `PREFLIGHT`, `VERIFY` and `SNAPSHOT`
(unchanged):

1. When the WordPress account cannot load the database as it stands, the
   administrator provisions it (`ensure`: create the database or the account,
   grant the account `ALL` on the database). Journaled `"database_created"`
   when it created the database.
2. `"load_started"`, then database's `load(..., replace=<database>,
   prune_prefix=<the site's table prefix>)`, as the WordPress account: every
   base table and view whose name starts with the prefix is dropped, then the
   Dump is loaded (database 0.3.5, `TransferResult.dropped`). The tables
   dropped are logged by name (`database_tables_dropped`).
3. The post-load check: the loaded database must be WordPress with a schema
   version; then `"loaded"`.
4. Only for a relocation (`--new-db-*`): the journaled cutover of the keys it
   moves, as before.

**Which database (decision 51).** The one `wp-config.php` names, resolved as
PHP would from the restore's own environment (`getenv_docker()` included), so
the restore runs with the site's variables (a sidecar given the WordPress
container's environment file). No confirmation flag: the resolved host and
name are logged and shown in the Dry Run (`LOAD_DUMP`'s line names the host,
port and database that will be overwritten, and every table that will be
dropped).

**Exact state (decisions 52 and 53).** A restore brings back exactly the
backed-up database state: after it, the database holds no table with the
site's prefix that the Archive did not have. Scope: only tables carrying the
site's table prefix; tables without it (another application, or another
WordPress with another prefix) are never touched. The prefix is matched
literally (database escapes `_` and `%`), and views are dropped before base
tables.

**Automatic rollback (decision 50).** On any failure inside `LOAD_DUMP` after
`"load_started"` (the load, the check, or a relocation key that did not land),
the live database is put back before the Run exits: the Safety Snapshot's Dump
is extracted into the Run workspace (decrypted with the Run's passphrase when
the snapshot is encrypted) and loaded the same way (`replace`, `prune_prefix`),
journaled `"rollback_started"` then `"rolled_back"`. A database that held
nothing before is emptied again instead (its prefixed tables dropped, or the
database dropped when this Run created it). The Run then exits with the load's
own error, and the failure is orderly (ADR-0021): the journal is finished.

If the rollback fails, or nothing can roll back (`--no-safety-snapshot` over a
database with content), `"rollback_failed"` is journaled, the Run stays
recorded as interrupted, and `RollbackFailedError` (exit 1) names both
failures, the Safety Snapshot and the recovery commands (resolve the Run, then
`wpbackup2 restore --wp-dir <root> --archive <snapshot> --mode db`).

Not rolled back: a SIGTERM or Ctrl-C (the Run stops at once and is recorded
interrupted; the undo text names the snapshot and the commands); a cutover
write whose landing is unknown, or one that wrote a key (resolution completes
the set, as before); and `SEARCH_REPLACE`, the phase after `LOAD_DUMP`, whose
undo is the relocation's own text.

**The administrator only when needed (decision 55).** The load checks run as
the WordPress account (`check_load(..., replace, prune_prefix)`). Its
blockers that only an administrator resolves (`database_absent`,
`user_absent`, `authentication_failed`, `missing_privilege`) call for the
administrator's `check_ensure`; with none configured the Run refuses at
`PREFLIGHT`, before anything changes (`AdminCredentialsRequiredError`, exit 4),
naming the database, the account and what is missing. Started as root, the CLI
resolves a configured administrator before it switches to the Site Root's
owner (decision 71), so an AWS profile in root's home is read.

**AD-29, amended.** The target is surveyed as the WordPress account. When it
logs in and sees its database, that answer stands: a successful login and a
visible schema are evidence about the database. A failed login, or a database
the account cannot see, is still no fact about the database: the
administrator surveys instead when one is configured, else the Run refuses
naming what is missing (`authentication_failed`, `database_not_visible`).

**`--force` (decision 57).** A database with tables, views, routines or events
but no table carrying the site's prefix does not look like this WordPress
site, and needs `--force`. A WordPress database or an empty one needs nothing:
the Safety Snapshot and the rollback cover the overwrite.

**`--drop-database` (decision 63).** There is no previous database left to
remove. The flag is accepted and has no effect (WARNING `deprecated_option`);
`--mode all --cleanfirst` alone now selects `all-clean`, and `--mode db
--drop-database` still names the Mode `database-clean`, which restores as
`database` does.

**Relocation.** `--new-db-name` and `--new-db-host` load the Dump into that
database the same way (`replace`, `prune_prefix`), and then the cutover writes
the keys the relocation moves. The live database is untouched; a relocated
database this Run created is dropped again after a failure
(`"load_target_dropped"`). A relocation key the target reads from the
environment is refused (`EnvironmentDrivenTargetError`), since writing it would
replace the lookup.

**Journal (AD-18, ADR-0020, ADR-0021).** The on-disk format is unchanged:
`staging_database` is always `null` now, `load_database` names the live
database (or the relocation's), `cutover_keys` holds a relocation's keys only.
The new steps are `"database_created"`, `"load_started"`, `"loaded"`,
`"rollback_started"`, `"rolled_back"`, `"rollback_failed"` and
`"load_target_dropped"`, each with an undo row derived when the journal is
read. A journal an earlier version wrote (staging steps) is read with its own
rows. `--resolve-interrupted-run` acknowledges a direct-load interruption and
logs `database_rollback_owed` with the snapshot and the recovery command; it
never restores (resolving and restoring are never one command).

**AD-28: the SQL is database's.** The prefix-scoped drop is database's
(`drop_tables`, `load(..., prune_prefix=)`, release 0.3.5); backup issues no
SQL. `DatabaseQuery.check_load` and `DatabaseCommand.load_dump` gain `replace`
and `prune_prefix`; `DatabaseCommand.drop_tables` is new, with its no-op twin.

**Dry Run (AD-4).** Unchanged in kind: the same queries run (`check_load` as
the WordPress account, `check_ensure` when the administrator is needed), the
commands are no-ops, and a Dry Run refuses where the real Run would.

## Consequences

- Positive: a restore of the official image's sites works and keeps their
  environment lookups; `--archive latest` finds the site's Archives after a
  restore, since `DB_NAME` no longer changes.
- Positive: the site's own account suffices for an existing site; an
  administrator is needed only to create a database or an account, or to
  grant a right.
- Positive: the database matches the Archive exactly (prefix-scoped), and
  nothing outside the prefix is touched.
- Negative / accepted: the site is down for the length of the load, and a
  failed load is recovered by a second full load (the snapshot). With
  `--no-safety-snapshot` there is no recovery; the error says so.
- Negative / accepted: a rollback that fails leaves the live database partly
  loaded; the Run is recorded interrupted and the error names the snapshot and
  the commands.
- Negative: the restore must run with the same environment as the site; a
  different environment restores into whatever database it names. The Dry Run
  and the log name the host and database (decision 51).

**Revisit when:** a supported engine or a host makes the outage of a direct
load unacceptable for typical sites (option A is then the design to revive,
with its database API), or WordPress core starts depending on unprefixed
tables.
