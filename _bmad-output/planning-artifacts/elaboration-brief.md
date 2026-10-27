# Elaboration brief — l3io-wordpress-backup, route 1 (2026-10-20)

Shared by every `l3io-story-enrich` batch. One batch per epic. Read this file, then the
files it names; widen only with a reason.

## What you are doing

Each story document under
`_bmad-output/implementation-artifacts/epic-{nnn}/sprint-{nn}/stories/` currently holds the
`epics.md` user-value ACs only. Append, below the existing `**Acceptance Criteria:**` block
and without editing it or the frontmatter, the two sections in the exact layout and order of
`.claude/skills/l3io-plan/references/ac-dimensions.md`:

```
## Business acceptance criteria
### Outcome
### Non-goals

## Technical acceptance criteria
### Interface contracts
### Error and edge case handling
### Observability requirements
### Security considerations
### Testability approach
### Existing-library check
```

Every dimension is filled or `N/A — <reason>`, never omitted. Treat each story on its own
terms; do not paste one answer across the batch.

## Provenance: write resolving `Spec:` pointers

A spec index exists and is fresh: `_bmad-output/implementation-artifacts/spec/spec-index.md`
lists every section as `path#anchor — heading (Lstart–Lend)`. Open only the ranges you need
(`sed -n 'START,ENDp' path`). End every applicable dimension, `Outcome` included, with
`Spec: <path>#<anchor>` copied exactly from the index (several allowed, one per line).
`Spec: none — <reason>` only when no section governs it. `Non-goals` takes no pointer.

## Why this is being done — read before writing

The E002 architecture gate (`_bmad-output/implementation-artifacts/epic-002/arch/arch-gate-review.md`)
blocked because stories never carried the spine's decisions as acceptance criteria. Its
**Class A table** lists, per finding, the decision and the spine section. Every Class A
finding that touches a story in your batch must become a concrete, testable criterion in
that story. That table is the checklist the re-run gate will read your output against.

The spine is `status: final`. You carry its decisions; you do not reopen them. If a story
genuinely cannot satisfy one, write it into that story under the relevant dimension as
`OPEN: <what and why>` and say so in your final line — do not invent a resolution.

## Decisions made 2026-10-20 that override older text

Older documents (the PRD, the gate record, `00-SHARED-CONTEXT.md`, superseded ADRs) predate
these. Where they disagree, these win:

1. **Database work goes through `l3io-wordpress-database` v0.1.0** (ADR-0015,
   `docs/adr/0015-credentials-delegated-to-database.md`). This package never invokes a MySQL
   client tool, writes an option file, or issues `CREATE DATABASE`/`GRANT`/`DROP DATABASE`.
   It calls: `dump(connection, credentials, to, *, recipients=None, options=())`;
   `load(connection, credentials, source, *, replace=None, identities=None, options=(),
   allow_unsandboxed=False)`; `survey(connection, admin, *, wordpress_user=None,
   user_host="%", table_prefix="wp_") -> Survey`; `ensure(connection, admin, wordpress, *,
   user_host="%", charset=..., collation=...) -> EnsureResult` (flags `created_database`,
   `created_user`, `password_set`, `granted`, plus `verdict`); `drop_database(connection,
   admin, *, confirm: str)`; `check_dump`/`check_load`/`check_ensure`/
   `check_drop_database`; `CredentialChain` (first present wins; a source that *failed*
   stops the chain). Source of truth: `../l3io-wordpress-database` at tag `v0.1.0`
   (`git -C ../l3io-wordpress-database show v0.1.0:src/l3io/wp/database/<file>.py`). Verify a
   signature there before writing it into a criterion.
   The call sites live in `adapters/database`, owned by Epic 2.
2. **Encryption is `age`, through database** (ADR-0014). Recipients/identities: database
   v0.1.0's `l3io.wp.database.encryption.encrypt(reader, writer, recipients)` /
   `decrypt(reader, writer, identities)`. **Passphrase** (AD-26's chain) arrives in database
   **0.2.0** as `passphrase=`; criteria that need it say it lands with that release, and that
   a passphrase-only configuration is refused until then, naming the release. No KDF
   parameters or key material in the Manifest; this package imports no `pyrage` and no
   cipher.
3. **Exit codes**: database's `cli.Exit` — 0 OK, 1 FAILED, 2 USAGE, 3 REFUSED,
   4 CREDENTIALS, 5 UNREACHABLE, 7 NOT_READY; 6 reserved for the family — plus backup-only
   codes for verification failure and lock conflict. The enum lives in `domain/`; its
   coverage test derives scope from the enum.
4. **Storage Target grammar**: `file://…` accepted, a bare path means `file:`, any other
   scheme a named error; in `domain/`, Epic 2.
5. **Backup Scope**: domain value `database`; CLI spelling `--mode db` (frozen, AD-22); one
   mapping in `cli/`.
6. **Ownership** is the `epics.md` Epic List table as of `f91261c`, including its ownership
   notes. A criterion must not require a story to edit a module another epic owns.
7. **Restore-path clauses** formerly in stories 2.3 and 2.7 now live in story 3.1.

## Files to read

- `.claude/skills/l3io-plan/references/ac-dimensions.md` — dimension guidance
- `_bmad-output/implementation-artifacts/epic-002/arch/arch-gate-review.md` — Class A table
- `_bmad-output/implementation-artifacts/spec/spec-index.md` — then only the ranges you need
- `_bmad-output/planning-artifacts/epics.md` — your epic's section and the Epic List
- `_bmad-output/implementation-artifacts/handoffs/00-SHARED-CONTEXT.md` — domain traps;
  where it conflicts with the decisions above, the decisions win
- Before writing technical ACs: list the files each story will add or change under
  `src/l3io/wp/backup/` (mostly empty placeholders today), inspect any existing siblings,
  and record a convention a new file must follow. `tools/check_purity.py` and
  `pyproject.toml`'s `[tool.importlinter]` contracts are the layer rules (AD-1) already
  enforced.

## Output

Write the story files only. Do not edit `epics.md`, the spine, ADRs, state YAML or any
other file. Your final line is exactly one of `DONE — <n> stories elaborated`,
`BLOCKED: <reason>` or `FAILED: <reason>`, preceded by one line per story naming any
`OPEN:` item.

## Epic 2 is elaborated (3b30baf) — reuse its names, do not invent parallel ones

Epics 3–7 build on contracts Epic 2's stories now define. Read the relevant E002 story
(`_bmad-output/implementation-artifacts/epic-002/sprint-0*/stories/`) before naming
anything it already names. Established there:

- Modules: `domain/{archive_format,errors,exit_codes,flag_map,site,storage_target,lock,backup_scope}.py`;
  `ports/{archive,database,lock,preflight,clock}.py`; `core/{backup,verify}.py`;
  `adapters/{tar,database,redaction,noop}.py`; `cli/{parser,context,logging,exit,scope,backup,verify}.py`.
- Types: `SiteDescriptor`, `RunContext` (+ `RunContextActiveError`, joining semantics),
  `ArchiveView`, `ArchiveProbe`, `ArchiveStore` / `LocalArchiveStore` / `NoopArchiveStore`,
  `VerificationResult` (carries `integrity`), `SiteLock` / `LockHandle` / `LockHolder`
  (re-entrant for the holder's Run), `ExitCode` (0,1,2,3,4,5,7 family; 8
  `VERIFICATION_FAILED`, 9 `LOCKED`; 6 reserved), the `BackupError` hierarchy in
  `domain/errors.py` (`RefusedError`, `CredentialsError`, `VerificationFailedError`, …).
- Conventions: every port is split into queries and commands (AD-4); a Dry Run binds
  no-op commands from `adapters/noop.py`; one redaction implementation in
  `adapters/redaction.py` that every epic reuses; `cli/exit.py::exit_code_for` is the one
  failure→code mapping, and **a later epic maps to an existing `ExitCode`, never adds one** —
  if it needs a new kind, write `OPEN:` naming it.
- Ownership: anything under `domain/`, `ports/`, `cli/`, `core/backup` and
  `adapters/{tar,database,store,lock,redaction,noop}` is Epic 2's. A later story that needs
  a change there writes it as a dependency on Epic 2 (`OPEN:` if Epic 2's stories do not
  already provide it), never as its own edit.

## Since round 1 (E002 gap pass ee18747, decisions a865618, 2026-10-27) — also binding

- **Epic 2 now also provides** (read the "Added 2026-10-20" parts of the E002 stories):
  `domain/restore.py` (`RestoreMode`, `RestorePhase`, `OverrideSet`, `SnapshotPolicy`);
  `domain/exclusion.py`; the Manifest `origin` and `exclusions` keys; `ports/crypto.MemberCipher`;
  restore ports `SiteFilesQuery`/`SiteFilesCommand`, `DatabaseQuery`/`DatabaseCommand`,
  `ConfigQuery`/`ConfigCommand` with no-op twins and `AdapterSet` fields;
  `cli/passphrase.py::resolve_passphrase` (AD-26: argument, then
  `L3IO_WP_BACKUP_PASSPHRASE`, then a TTY prompt); the Run-Context-joining entry point
  `cli/backup.py::backup(site, scope, target, *, ctx, cipher, exclusion, origin)`;
  `site_descriptor_from_archive_config`. E002-S02-009 implements the restore adapters,
  including `adapters/config_writer` (the only caller of `l3io.wp.config.WpConfigFile`).
- **Exit codes route by class attribute**: each `BackupError` subclass declares
  `exit_code: ClassVar[ExitCode]`. A later epic declares it on its own subclasses and never
  edits `cli/exit.py`. Still: no new `ExitCode` member outside Epic 2.
- **Flags**: `all-clean` needs `--cleanfirst` (files) **and** `--drop-database` (database);
  `--force` only waives the non-empty-target refusal. Epic 2 owns `cli/parser.py`; a later
  epic that needs a flag writes `OPEN:` naming it rather than adding it.
- **Ownership changes**: `adapters/config_writer` is Epic 2's (Epic 7 calls it);
  `cli/restore.py` and its dispatch arm are Epic 3's (E003-S01-006). Every other `cli/`
  module is Epic 2's.
- **Dependencies, current pins**: `l3io-wordpress-database` at rev `47384ba` (v0.1.0 plus
  `passphrase=` on `encrypt`/`decrypt`/`dump`/`load`; import `encrypt`, `decrypt`,
  `check_passphrase` from the package root — verify with
  `git -C ../l3io-wordpress-database show 47384ba:src/l3io/wp/database/<file>.py`);
  `l3io-wordpress-config` **v0.1.1** (`set()` keeps mode and owner, fsyncs, raises
  `ConfigWriteError` rather than change the owner, **never creates** a `wp-config.php`).
- **Spine amendments** (read via the spec index): AD-17 (the `DB_NAME` cutover is
  `LOAD_DUMP`'s last journaled step), AD-18 items 3–4, AD-22 (versions restart; first
  release v0.0.1), AD-23 (`wp-root/` alone is a files-only v0), AD-26 (unencrypted Snapshot
  proceeds with a warning; explicit opt-out).
- `pathspec` and `platformdirs` are already accepted dependencies. A new one needs its
  Existing-library check and the library's current version verified.
