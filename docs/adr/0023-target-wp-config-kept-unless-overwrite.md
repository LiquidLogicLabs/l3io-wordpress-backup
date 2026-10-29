# ADR-0023: The target's `wp-config.php` is kept unless `--overwrite-wp-config`

- **Status:** Accepted
- **Date:** 2026-10-28
- **Deciders:** Shawn Anderson
- **Epic:** E003
- **Resolves:** arch gate finding AG-16 (2026-10-28)
- **Departs from spec:** _bmad-output/planning-artifacts/prds/prd-py-wordpress-backup-2026-09-12/prd.md#52-restore
- **Principle(s) in tension:** FR-6 ("each mode does exactly what its name says"), AD-18.2 (atomic config write), AD-18.3 (the live connection changes only at the cutover), AD-22 (new flags allowed, none changed), NFR-1

## Context

E003-S02-005 excluded an existing `wp-config.php` from extraction, so that the only writer
of a live config is `l3io-wordpress-config`'s atomic `set()`. The consequence was a
`spec-gap` (AG-16): a `files` or `all` restore never restored the target's config. That
contradicts FR-6's "does exactly what its name says" and E003-S01-001's "restored parts
match the Archive", and the operator was not told.

Overwriting the config blindly is not safe either. The Archive's config carries the
*source's* `DB_*` values, so laying it down points the site at the source's database. It
also replaces salts and paths.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Always keep the target's config, silently | No new write path | FR-6 gap; operator not told | Departs from FR-6 unannounced |
| B. Always restore the Archive's config | FR-6 literally | Site points at the source's database; salts and paths change on every restore | Breaks AD-18.3's guarantee |
| C. **Keep by default and say so; replace only on an explicit flag, with the `DB_*` keys set back to the target's** | Safe default, visible; the full restore is reachable | One new flag, one new write path | FR-6 met by an announced default and an explicit opt-in; AD-22 permits the flag |

## Decision

**Option C.** `ConfigPolicy` is a closed `StrEnum` in `domain/restore.py` (E002-S02-011),
`KEEP = "keep"` and `OVERWRITE = "overwrite"`.

**Default, `ConfigPolicy.KEEP`.**

- When the target has a `wp-config.php` at the located path (`LocatedConfig.path`), a
  restore never replaces it. The site-root extraction excludes `wp-config.php`, so no second
  config is laid down beside a `PARENT` one.
- In modes that restore the database, the config changes only through the cutover's
  `DB_*` keys.
- The Run prints one info line, `wp-config.php kept (target's); pass
  --overwrite-wp-config to replace it`, and logs `wp_config_kept`.

**The flag.**

- `--overwrite-wp-config` is a new flag (AD-22 allows new flags; no existing flag changes).
  E002-S02-011 registers it in the `restore options` group. It maps to
  `RestoreRequest.config_policy: ConfigPolicy`. The field is keyword-required with no
  default (AD-5 style); the CLI default is `KEEP`.
- It is valid only in modes that restore files: `files`, `files-clean`, `all`, `all-clean`.
  In `database` or `database-clean` it is a `UsageError` (exit 2).
- If the Archive has no config member, `OVERWRITE` is refused at plan time, before any
  effect, with `ArchiveHasNoConfigError(RefusedError)` (exit `REFUSED`).

**`OVERWRITE` behaviour, in the `REWRITE_CONFIG` phase.**

1. Copy the target's current config, mode `0600`, into the Run's journal directory (the
   per-Site state directory, ADR-0021), as `<run_id>-wp-config.php`. That copy is the undo.
   Journaled as step `"config_saved"`.
2. Read the Archive's config member with `ArchiveProbe.read_member` into the workspace.
3. Read the target's four `DB_*` values through `ConfigQuery.read_constants`. The
   `DB_PASSWORD` is registered with the Run's `SecretRegistry`, held in memory only until
   the write, and never journaled.
4. `ConfigCommand.replace_file(config_file, source, keep=<those four values>)` applies `keep`
   to the Archive's config text **in memory**, with `l3io-wordpress-config`'s public
   `WpConfigString.set` (v0.1.2 or later: earlier versions raise on CRLF content), reading
   and writing with `newline=""` so the file's line endings are kept, then writes a temp
   file in the target config's directory, fsyncs
   it and `os.replace`s it onto the located path. The target file's mode and owner are
   kept, and the write refuses rather than change the owner, as `l3io-wordpress-config`
   does. Journaled as step `"config_replaced"`. The file that lands carries the target's
   own `DB_*` values, in every mode. No `DB_*` key is written separately, and the site
   never points at the source's database, not even between this phase and the cutover.
   In `all` and `all-clean`, E003-S02-005's cutover then runs exactly as under `KEEP`.
5. WARNING `wp_config_overwritten`: salts, paths and other constants now come from the
   Archive.
6. `undo_for` (ADR-0020) derives "copy `<saved>` back to `<config path>`" from the
   `"config_saved"` and `"config_replaced"` steps (`REWRITE_CONFIG`).

The two file effects (steps 1 and 4) are `ConfigCommand` methods (E002-S02-012),
implemented in `adapters/config_writer.py` (E002-S02-009) with no-op twins, so a Dry Run
copies and replaces nothing.

**Other cases.**

- If the target has no `wp-config.php`, the ADR-0016 amendment applies: the Archive's
  config is used, whatever the flag says.
- When the config is located one level up (`PARENT`), the flag replaces the located path.
- `docs/restore.md` (created by E003-S01-001) documents `KEEP`, the flag and its WARNING.

## Consequences

- Positive: the default is safe and visible; a full restore of the config is one explicit
  flag away, and still ends on the target's database.
- Negative / trade-off accepted: a second write path for a credential-bearing file. It is
  one function behind one command port, with the same temp-fsync-replace sequence and
  owner rule as `l3io-wordpress-config`. The flag opts into it.
- AD-17's "no `DB_*` write before the end of the load" holds with no exception: the
  `DB_*` values in the replaced file are the target's own, set in memory before the write.
- Follow-ups: E002-S02-011 (flag and `ConfigPolicy`), E002-S02-012 (the two `ConfigCommand`
  methods and their no-ops), E002-S02-009 (the real implementation), E003-S01-001 (`KEEP`
  exclusion, `OVERWRITE` arm, integration cases, `docs/restore.md`), E003-S02-005 (the
  cutover's key set under `OVERWRITE`), E003-S02-006 (request field and usage check). The
  PRD's FR-6 section links here.
- **Revisit when:** `l3io-wordpress-config` offers a whole-file atomic replace. The write
  then moves there, as AD-18.2 prefers.

## Concern — resolved 2026-10-28

The first draft wrote the Archive's file first and put the target's `DB_*` values back
afterwards: through `set()` in the files modes, and at the cutover in `all`/`all-clean`. In
the database modes that left a window, between `REWRITE_CONFIG` and the cutover, in which
the file on disk carried the source's connection values. Applying `keep` in memory before
the atomic replace (step 4) removes the window without any `DB_*` write before the load.
Shawn's stated invariant, that the site never points at the source's database, holds
throughout.
