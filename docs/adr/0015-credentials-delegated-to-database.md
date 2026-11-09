# ADR-0015: Credentials reach MySQL client tools only through `l3io-wp-database`

- **Status:** Accepted
- **Date:** 2026-10-20
- **Deciders:** Shawn Anderson
- **Epic:** E002 (stories 2.1, 2.4), E003 (stories 3.4, 3.5), E005 (story 5.1)
- **Supersedes:** [ADR-0003](0003-credential-transport-option-file.md)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-7--credentials-reach-a-subprocess-by-file-never-by-argument-or-environment

## Context

ADR-0003 had this package write a `[client]` option file at mode `0600` into the per-Run
workspace and pass it as `--defaults-file`. `l3io-wp-database` v0.1.0 owns `dump` and
`load`, and its ADR-0002 passes the same options through an anonymous pipe
(`--defaults-file=/dev/fd/N`) with a scrubbed environment and, on MySQL 8.4+,
`--no-login-paths`. Nothing reaches disk, a crash leaves nothing behind, and a hostile
`~/.my.cnf` or `~/.mylogin.cnf` changes nothing — stronger than a file on disk.

Two transports in one family would mean two implementations of the guarantee, and only
one tested against the tools.

## Decision

This package **never builds an argument vector for, or an option file for,
`mysqldump`/`mysql`/`mariadb-dump`/`mariadb`**. It calls `l3io-wp-database`:

- `dump` and `load` (with `replace=None`; corrected 2026-10-27, see the amendment) for the
  database half of backup and restore;
- `survey`, `ensure` and `drop_database` for provisioning — AD-29's existence checks and
  the spine's "exactly one provisioning implementation";
- `CredentialChain` for credential providers (story 5.1).

AD-7's invariant stands for this package: no credential in any log record, argument
vector, `repr` or exception raised to a caller. Its **mechanism** and the assertions about
the option file now belong to `l3io-wp-database` (its ADR-0002 and tests). This package
keeps the assertions on what it emits itself, and a test that no module here invokes a
MySQL client binary.

## Consequences

- One credential transport for the family, owned where the client tools are called.
- Story 2.4 shrinks to composing database's API and asserting this package's own output;
  it no longer needs the per-Run workspace for an option file.
- Restore depends on database's `load` semantics, including its refusal to load over a
  non-empty database unless `replace=` confirms it (corrected 2026-10-27).
- **Revisit when:** this package needs a client-tool invocation database does not offer.
  Add it to database rather than here.

## Amendment — 2026-10-27: who constructs credential sources, and `replace=`

Accepted by Shawn, 2026-10-27, resolving arch gate finding AG-43 and the gate's backlog
MINOR on `replace=`.

- **Credential construction happens only in `adapters/database.py`**, and in Epic 5's
  `adapters/credential_providers.py`, which builds the provider chains. Only those modules
  instantiate database's `Explicit`, `Environment`, `AwsSecretsManager`, `WpConfigFile` or
  `CredentialChain`. This keeps `adapters/database.py` as "the one place that calls
  `l3io-wp-database`" (E002-S01-001), with Epic 5's providers module as its named
  companion.
- **`cli/` and `api/` pass backup-owned values, never database types.** A composition root
  turns flags or library arguments into a backup `CredentialSpec` value object defined in
  `domain/` (for example: explicit username and password, a `wp-config.php` path, an AWS
  secret id and region). The adapter turns that value into a database source. The exact
  fields are the story's to define. `cli/credentials.py` (E002-S01-006) delegates to the
  adapter; it builds no database class itself.
- **Database's `CredentialSource` never appears on this package's public API** (Epic 6).
  `BackupRequest.credentials` and its restore counterpart carry the backup-owned value. A
  dependency type crossing the public boundary is what the Consistency Conventions and
  AD-14 forbid.
- **`replace=` corrected.** The Decision's first bullet said `load` is called "with
  `replace=`", and the Consequences called `replace=` "staging". Both were wrong.
  Database's `replace=` is a **confirmation token**: it must equal the target database's
  name before `load` will load over existing tables (verified against database v0.2.0,
  `transfer.py`: a non-empty target without it is the `DATABASE_NOT_EMPTY` finding). It is
  not a rename and not a staging mechanism. This package calls `load` with
  `replace=None`. The two sentences above are corrected in place and marked.
- **Revisit when:** a caller needs to pass a credential provider this package does not
  model in `CredentialSpec`. Add the case to the backup-owned value; do not expose
  database's type.

## Amendment — 2026-11-09: `load` with `replace` and `prune_prefix` (Shawn, owner decisions 49 and 53)

A restore now calls database's `load` with `replace=<the live database>` and
`prune_prefix=<the site's table prefix>` (l3io-wordpress-database 0.3.5), as the WordPress
account, so the Dump loads over the live database after every table carrying the prefix was
dropped ([ADR-0027](0027-restore-into-an-environment-driven-database.md), Accepted). The
"`load` with `replace=None`" rule above no longer holds for a restore; credentials still reach
the client tools only through database.
