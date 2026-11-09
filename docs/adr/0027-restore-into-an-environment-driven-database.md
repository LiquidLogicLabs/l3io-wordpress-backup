# ADR-0027: Restoring a site whose `DB_NAME` comes from the environment: stage, then swap the tables into the live name

- **Status:** Proposed
- **Date:** 2026-11-08
- **Deciders:** Shawn Anderson (decision pending)
- **Epic:** close-out (owner decision 47, restore half)
- **Resolves:** owner decision 47, "Restore, for an env-driven `DB_NAME`"; the sidecar test's phase C (2026-11-08)
- **Departs from spec:** ADR-0022's cutover (the site switches databases by rewriting `wp-config.php`'s `DB_NAME`), for this case only
- **Principle(s) in tension:** ADR-0022 (the live site stays intact if the load or its check fails), AD-28 (backup issues no SQL; database does), AD-4 (a Dry Run refuses where the real Run would), AD-18 (every effect is journaled and can be resolved)

## Context

A restore that loads the database never loads over the live database. It loads the Dump into
a new staging database, `<live name, first 51 characters>_l3s_<first 8 of the Run ID>`, checks
that it is a WordPress database, and then switches the site to it by rewriting
`wp-config.php`'s `DB_NAME` (the cutover, ADR-0022). The live database is kept (`database`,
`all`) or dropped (`database-clean`, `all-clean`).

The official `wordpress` Docker image defines its settings from the container's environment:

```php
define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );
```

Rewriting that line replaces the lookup with a fixed string. The site then stops following its
environment, and the next `docker compose up` with a changed `WORDPRESS_DB_NAME` silently
points somewhere else. The tool cannot change the variable instead: it belongs to another
container's definition (a compose file, a Kubernetes manifest), which the tool does not own.
So for such a site the restored data must end up **in the database the environment already
names**.

Until this ADR is accepted, such a restore is refused before any change, a Dry Run included
(`EnvironmentDrivenTargetError`, exit 3; `docs/restore.md`). A files-only restore that keeps
the target's `wp-config.php` is not affected. Backup and `doctor` resolve the lookups from
their own environment (owner decision 47, backup half; `l3io-wordpress-config` 0.1.5's
`get_value` and `EnvLookup.resolve`).

`DB_HOST`, `DB_USER` and `DB_PASSWORD` read from the environment do not block the existing
cutover, which rewrites only `DB_NAME`. They block relocation (`--new-db-host`,
`--new-db-user`, `--new-db-password`), which rewrites them, and `--overwrite-wp-config`
(owner decision 48, which needs `l3io-wordpress-config` to write a value's source text). The
interim refusal covers all four `DB_*` for simplicity; this ADR narrows it.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. **Load into the staging database as today, check it, then move its tables into the live database name with one multi-table `RENAME TABLE`**, the live tables moving to a kept-aside database in the same statement | The live site is intact until one atomic statement; no data is copied (a rename within a server is a metadata change); the old tables stay for undo | New database API and privileges; views, triggers, routines and events need rules; an interrupted Run needs a new resolution path | ADR-0022's guarantee kept; AD-28 kept if the statement is database's |
| B. Load directly into the live database after the Safety Snapshot (drop its tables, load) | Simplest; no new SQL | The site is broken for the whole load; a failed load leaves a half-loaded live database, recoverable only by restoring the Snapshot (another full load) | Breaks ADR-0022's guarantee |
| C. Rename the database | One name change | MySQL and MariaDB have no `RENAME DATABASE` | Not available |
| D. Leave the swap to the operator: load into staging, print "set `WORDPRESS_DB_NAME` to `<staging>` and restart" | No new SQL | The restore ends half done by design; the site and the environment disagree until someone acts | Breaks FR-9's "restored" report |
| E. Rewrite `wp-config.php` anyway | Works today | Turns the lookup into a constant: the defect this ADR exists for | Rejected by decision 47 |

## Decision (proposed)

**A.** For a target whose `DB_NAME` is an environment lookup, the cutover is a table swap
instead of a `wp-config.php` rewrite. Everything before it is unchanged.

**Which database.** The live name is `DB_NAME` as the restore resolves it from its own
environment, so the restore must run with the site's variables (a sidecar given the WordPress
container's environment file, as the sidecar test does). The plan names the resolved host
and database. The live database must exist and survey as a WordPress database with the
Site's `$table_prefix`; otherwise the restore is refused, because a different environment
would otherwise swap tables into the wrong database. Whether to also require an explicit
confirmation flag, as `--confirm-db-host` does for a backup, is open question 3.

**Sequence (`LOAD_DUMP`).**

1. Ensure and load the staging database, then check it, exactly as today.
2. As the administrator, create the aside database `<live name, first 51>_l3a_<first 8 of the Run ID>`
   with the live database's character set and collation.
3. Inventory the base tables of the live and staging databases.
4. **One statement**, as the administrator:
   `RENAME TABLE live.t1 TO aside.t1, …, staging.t1 TO live.t1, …`: every live table to
   the aside database and every staging table into the live one. Either all renames happen
   or none.
5. Drop the now-empty staging database.

`wp-config.php` is not written. The WordPress account's grant is database-level
(`GRANT ALL ON live.*`, as `ensure` writes it and the official images create it), so it
covers the tables that arrive. Table-level grants on the old tables would not follow them;
the preflight refuses when the account holds any.

**Mode semantics.** `database` and `all` keep the aside database, as they keep the old live
database today, and the report names it. `database-clean` and `all-clean` drop it after the
swap. There is nothing to revoke, because the account never had a grant on the aside name.

**Privileges.** `RENAME TABLE` needs `ALTER` and `DROP` on each source table, and `CREATE` and
`INSERT` on each destination. The administrator therefore needs those on the live, staging
and aside databases, plus `CREATE` for the aside database. `check_ensure`'s administrator
check grows to include them. The WordPress account needs nothing new.

**AD-28: the SQL is database's.** Backup issues no SQL (`test_no_sql_ddl`). This needs two new
functions in `l3io-wordpress-database`, a query and a command in its usual pair, for example
`check_swap_tables(connection, admin, staging=, aside=)` returning a `Verdict` and
`swap_tables(…, confirm=)` returning the moved names. Backup adds them to `DatabaseQuery`
and `DatabaseCommand`, with no-op twins. This is a cross-repository request and the first
implementation step.

**MySQL and MariaDB.** Both accept a multi-table `RENAME TABLE` that moves tables between
databases on one server, and both refuse to move a view to another database or a table that
has triggers. Both document a multi-table rename as all-or-nothing on error, and both make it
crash-safe through atomic DDL: MySQL 8.0 for InnoDB, MariaDB from 10.6. On a server without
atomic DDL (MariaDB before 10.6, or non-InnoDB tables), a crash in the middle can leave a
partial swap. The resolution path below handles that case, and the integration matrix must
prove the all-or-nothing behaviour on every engine the release supports before this ADR is
accepted. These statements come from the vendors' documentation and are to be confirmed by
those tests, not assumed.

**Views, triggers, routines, events, foreign keys: refused in the first version.** The
preflight surveys both databases. `DatabaseTargetState` already counts routines and events,
and database's survey extends to views, triggers and foreign keys. It refuses (exit 3, naming
what was found) when either database has:

- views or triggers (a rename cannot move them);
- routines or events (they belong to the database, not its tables, so the old ones would stay
  with the new data);
- foreign keys, until a test shows that references among tables moved in the same statement
  stay correct on both engines.

WordPress core uses none of them. A later revision can re-create views and triggers from their
definitions after the swap, which would not be atomic.

**Journal and undo (AD-18, ADR-0020, ADR-0021).** The header records the swap plan: the live,
staging and aside names and the two table lists (names only). There are new steps,
`swap_started` and `swap_completed`. The undo text, derived when the journal is read, is the
reverse swap: move the live tables back to staging, then the aside tables back to live. For
the `-clean` Modes the undo is the Safety Snapshot, since the aside database is dropped.

**An interrupted Run.** With atomic DDL the swap happened completely or not at all.
`--resolve-interrupted-run` inventories the three databases:

- the live tables equal the staging plan: the swap happened, so it completes the remaining
  steps (drop staging, and for `-clean`, drop aside);
- the live tables equal the old inventory: the swap did not happen, so it abandons the swap
  (`cutover_abandoned`) and leaves the live site as it was;
- anything else, which only a server without atomic DDL can produce: it refuses, names the
  three databases and their tables, and points to the Safety Snapshot.

**A failed swap.** The statement either fails as a whole or times out waiting for the
metadata locks that the site's open transactions hold (bounded by `lock_wait_timeout`, which
database sets for the session). Either way nothing moved, the live site is intact, and the
staging database is dropped as after a failed load check today.

**Dry Run (AD-4).** It runs `check_swap_tables`: privileges, the inventories, the refusals
above, and that the aside name is free. The no-op command records the swap and returns
"not performed", so the Dry Run refuses exactly where the real Run would (ADR-0022).

**Space.** No second copy is needed beyond the staging database the load already needs. A
rename within one server's data directory moves files and copies no rows. A server that puts
databases on different file systems would copy the data, which is not supported.

**What stays refused.** Relocation that would rewrite an environment-driven `DB_HOST`,
`DB_USER` or `DB_PASSWORD` stays refused, for the same reason as `DB_NAME`, and so does
`--overwrite-wp-config` onto such a target until `l3io-wordpress-config` can write source text
(owner decision 48). An environment-driven `DB_HOST`, `DB_USER` or `DB_PASSWORD` with a
literal `DB_NAME` no longer blocks the existing cutover.

## Alternative B in more detail (rejected)

Loading directly into the live database (drop its tables, load the Dump, check) takes no new
SQL and no new privileges. It is what an operator does by hand with `mysql < dump.sql`. But
the site serves a database that is half loaded for the whole load, and a Dump that fails
partway or fails its check leaves the live database broken. The only recovery is the Safety
Snapshot, a second full load, which can itself fail. With `--no-safety-snapshot` there is no
recovery at all. It gives up the property ADR-0022 was written for. It would be the fallback
only if `RENAME TABLE` proved unworkable on a supported engine.

## Consequences

- Positive: the official image's sites can be restored and keep following their environment.
  The live site stays intact until one atomic statement, as today.
- Positive: undo is a rename back, with no reload.
- Negative: new database API (two functions), new administrator privileges, a new resolution
  path, and new journal steps.
- Negative: the first version refuses databases with views, triggers, routines, events or
  foreign keys.
- Negative: the restore must run with the same environment as the site. If the environments
  differ, the swap targets whatever database this environment names, and the WordPress survey
  is the only guard (open question 3).

## Open questions for the owner

1. Accept A, with the new database API as its first step?
2. Should views, triggers, routines, events and foreign keys be refused in the first version
   (as proposed), or should their re-creation be implemented first?
3. Should an explicit confirmation of the resolved `DB_NAME` be required for this path, like
   `--confirm-db-host`?
4. Is `<live>_l3a_<run>` the right name for the kept-aside database, and should the `-clean`
   Modes drop it immediately?

**Revisit when:** a supported engine fails the all-or-nothing test, or WordPress core starts
shipping views, triggers or foreign keys.
