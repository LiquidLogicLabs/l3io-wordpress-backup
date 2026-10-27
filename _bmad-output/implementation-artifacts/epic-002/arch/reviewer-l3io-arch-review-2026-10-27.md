# Arch gate re-review — E002 "Walking skeleton — take a backup and prove it"

- **Date:** 2026-10-27
- **Reviewer:** `l3io-arch-review`, Mode B (architectural review of an existing design)
- **Standards applied:** `standards-core.md` §1–10, `standards-python.md`
- **Scope:** the 9 story files under `_bmad-output/implementation-artifacts/epic-002/{sprint-01,sprint-02}/stories/`, checked against the spec sections they point to, the Epic List, ADR-0014, ADR-0015, `pyproject.toml`'s import-linter contracts and dependency pins, and the E002 `depends_on` edges in `state/planned/epic-002/`.
- **Previous record:** `arch-gate-review.md` (2026-09-27, BLOCKED, AG-1…AG-29)

## Executive summary

The remediation worked. Of the 29 previous findings, 28 are closed and 1 (AG-28) is closed for its original cause but has come back in a new form. Every story now carries technical ACs with Spec pointers, and the spine decisions that were missing (AD-3, AD-4, AD-6, AD-12, AD-13, AD-14, AD-16, AD-19–AD-26) are now in the stories as ACs with mechanical checks. Their scope is derived from the source (walked packages, parser introspection, enums), not from hand-kept lists. **This review finds 0 BLOCKER, 8 MAJOR and 11 MINOR.**

Most of the MAJORs come from the two "gap pass" additions (2026-10-20 and 2026-10-27). Each one is sound locally but was layered onto an earlier contract without reconciling it:
- The Run Context is frozen and holds adapters that are built *after* it opens.
- The exit-code list and the class-attribute mechanism disagree.
- The redaction hook puts an outward import in `domain/`.
- Story-level dependencies point forward into sprint 02, and two stories form a cycle.
- The stories describe `l3io-wordpress-database` v0.1.0 while the pin is now v0.2.0.

**Most important action:** reconcile the `open_run`/`AdapterSet`/`drive_backup` order in E002-S01-004 against E002-S02-007 and E002-S02-008 before sprint 01 starts. E003 and E006 copy this shape directly.

## Findings

MAJOR: [Core §3 contract coherence; spec AD-16, FR-28] The Run Context's adapter set is needed before it exists. E002-S01-004 §Interface contracts declares the frozen `RunContext.adapters: AdapterSet` and `open_run(*, adapters: AdapterSet, …)`. Its own `drive_backup` (gap pass 2) then orders the steps as: `join_or_open(ctx, adapters=..., site_root=...)` (acquires the Site Lock) → `locate_wp_config` → descriptor → `cli/credentials.wordpress_source` → `real_adapters(...)`/`dry_run_adapters(...)`. E002-S02-007 §Interface contracts binds credential sources into adapters at construction, and E002-S02-008 §"Acquired by the outermost composition root" requires the lock before `wp-config.php` is read. As written, the adapters that `open_run` requires cannot be built until after `open_run` has run. Remediation: pick one shape and state it in S01-004, S02-007 and S02-008. Either (a) `open_run` takes an adapter *factory* that is called after the lock, with `RunContext.adapters` set once through a documented two-phase constructor, or (b) `AdapterSet` leaves the Run Context and is passed beside it to `backup(...)`. Recording (b) would be a departure from AD-16's "bound adapter set" and needs an ADR. Then align the `drive_backup` step list and `tests/unit/cli/test_lock_order.py` with the chosen shape.

MAJOR: [Core §3, frozen §8 exit-code contract; spec AD-14, FR-21] The exit-code mapping contradicts itself inside E002-S01-005 §Interface contracts. The first mapping bullet sends "`UnsupportedStorageSchemeError`, existing final Archive name, `RunContextActiveError` → `REFUSED`". The decided mechanism (class attribute `exit_code`, "no `match` over classes") sends every class that does not override to `FAILED`. Meanwhile E002-S01-001 §Interface contracts defines `UnsupportedStorageSchemeError` and `ArchiveWriteError(reason="exists")` as plain `BackupError` subclasses, and E002-S01-004 defines `RunContextActiveError(BackupError)`. None of them derive from `RefusedError`. A dev will make the codes either match the list or match the mechanism, and the exit codes are a frozen CLI surface. Remediation: delete the superseded `match` list from S01-005 and state each class's code once. Make `UnsupportedStorageSchemeError`, the "final name exists" error (a new `ArchiveExistsError(RefusedError)` rather than `ArchiveWriteError(reason=…)`) and `RunContextActiveError` subclasses of `RefusedError` in S01-001 and S01-004, or deliberately leave them `FAILED` and say so.

MAJOR: [Core §1 layering; spec AD-1, import-linter "the pure layers never face outward"] E002-S01-006 §Interface contracts ("Clean exceptions") requires `BackupError.__str__` to "pass its message through the Run's `redact` when one is active". `BackupError` lives in `domain/errors.py` (E002-S01-001). `SecretRegistry`/`redact` live in `adapters/redaction.py`, and the Run Context lives in `cli/context.py`. `domain/` may import neither, so the obvious implementation fails `lint-imports`. Remediation: either drop redaction from `__str__` (the primary control is already "messages are built from structured fields, never a credential", AD-14), or specify the seam: a `domain/`-level `ContextVar[Callable[[str], str]]` that defaults to identity and is set and reset by `open_run`. Then state that `tools/check_purity.py` permits it.

MAJOR: [Core §1 single ownership, AD-14; spec epics.md#epic-list ownership note, ADR-0015] Three statements conflict about where `l3io.wp.database` may be called:
- E002-S01-001 §Interface contracts and the Epic List make `adapters/database.py` "the one place that calls `l3io-wp-database`".
- E002-S01-006 (gap pass 2) has `cli/credentials.py` construct `Explicit`/`CredentialChain` (and its contract test names `WpConfigFile` as the only class `cli/` may build *there*). E002-S02-009 has "the composition root" build the administrator `l3io.wp.database.Explicit`.
- E002-S01-004's public-facing `BackupRequest.credentials: CredentialSource | None`, which Epic 6's `api/` consumes, then carries a dependency type across the package's public boundary. The Consistency Conventions row "never a dependency type across a boundary" forbids that.

Remediation: either (a) move source construction behind an adapter function (`adapters/database.py::wordpress_source(...)`, with `cli/credentials.py` delegating), or (b) amend the ownership note to name `cli/credentials.py` as the second permitted caller. In both cases, give `BackupRequest`/the API a package-owned credential type (or a `ports/` Protocol) instead of database's `CredentialSource`.

MAJOR: [Core §7 dependency baseline; spec ADR-0014] The stories describe a dependency the project no longer pins. `pyproject.toml` now pins `l3io-wordpress-database>=0.2.0,<0.3` at rev `48a7da9…` (commit 49e99de). The stories still say:
- "verified against tag `v0.1.0`" for `dump` (`transfer.py` L476), `check_dump`, `survey`/`ensure`/`load`/`revoke`/`drop_database` line numbers and `cli.Exit` (E002-S01-001, S01-005, S02-007, S02-009);
- "at the current pin (`47384ba`)" (E002-S01-001 gap pass 2, `stored_siteurl`);
- "passphrase encryption waits for `l3io-wp-database` 0.2.0" (E002-S01-001 §Non-goals);
- passphrase requests on database < 0.2.0 are refused, and the `credential_sentinel` passphrase variant asserts that refusal "until 0.2.0" (E002-S01-006). Under the current pin, that test exercises a path that no longer exists.

Remediation: re-verify every cited signature and line at the pinned rev. Replace version-specific prose with "at the pinned rev" plus one place recording the verified rev. Restate the passphrase-refusal edge case and the sentinel variant against 0.2.0. Re-check the `stored_siteurl` claim against `48a7da9`.

MAJOR: [Core §4 testability / sequencing; plan `depends_on`] Story dependencies run forward and in a cycle:
- E002-S01-004 (sprint 01) `drive_backup`, `tests/unit/cli/test_tracing_hooks.py` ("a recording `SiteLock`"), `test_backup_entry.py` ("acquires no lock … (E002-S02-008)") and `RunContext.site_lock: SiteLock | None` / `adapters: AdapterSet` all need types and constructors that E002-S02-007 (`AdapterSet`, `real_adapters`/`dry_run_adapters`) and E002-S02-008 (`SiteLock`, lock in `open_run`) deliver in sprint 02. That is AG-28's shape again.
- E002-S01-006 uses `UsageError` (`PassphraseUnavailableError(UsageError)`) and "the accepted-and-explained set (E002-S01-005)", and relies on S01-005's flag registrations (`--encrypt-with-passphrase`, `--db-credentials-aws-*`). Yet state has S01-005 `depends_on` S01-006, so the two form a cycle.

Remediation: either move `AdapterSet`, the `SiteLock` Protocol and `LockHolder` type definitions into S01-004 (sprint 01), leaving S02-007/S02-008 to add only implementations and the no-op set, or move the S01-004 drive/hook ACs that need them into S02-007/S02-008. Break the S01-005↔S01-006 cycle by moving `UsageError` and the accepted-and-explained registry into S01-006 or an earlier story, and fix the `depends_on` edges.

MAJOR: [Core §3; spec AD-4, FR-19 "A Dry Run performs Verification"] E002-S02-007 §Interface contracts does not define what a backup Dry Run *executes* for the steps whose inputs come from no-op commands. `NoopDatabase.dump_to` writes no Dump, yet `WRITE_ARCHIVE` (`adapters/tar.py::write_archive`, a plain adapter function, not a port in `AdapterSet`) would read and hash that missing Dump file. It would also stream the whole Site Root into `os.devnull`. The `VERIFY` step has no staged Archive to verify. The story says only that the plans are equal and the no-ops record `skipped_dry_run`. A dev will either add an `if dry_run` (forbidden) or hit a crash on the missing Dump. Remediation: put the archive writer behind an `ArchiveWriterCommand` port (with a `NoopArchiveWriter`), or state that the plan executor runs only query steps and no-op command steps in a Dry Run and that `WRITE_ARCHIVE`/`VERIFY`/`PUBLISH` are reported `skipped_dry_run`. Also say what "performs Verification" means for a backup Dry Run (e.g. the store's `exists` query and the target's writability).

MAJOR: [Core §4 testability / security gate; spec NFR-3, ADR-0010] E002-S02-009 contradicts itself on the `wp-config.php` mode gate. §Interface contracts says `l3io-wordpress-config` v0.1.1 (`4c842ca`) is pinned and already verified to keep `0640`. §Testability marks the same mode test, and the fsync spy, `xfail(strict=True, reason="… v0.1.0 does not preserve mode; fixed in <release>")`. At the current pin both XPASS, so a strict xfail fails CI at once. The likely "fix" (dropping `strict` or the test) removes what §Security calls "the release gate for restore". Remediation: make both tests plain assertions against the pinned v0.1.1, and remove the xfail wording and the `<release>` placeholder.

MINOR: [Consistency Conventions "Data — timestamps"] E002-S01-001 writes `Manifest.created_at` with a `+00:00` offset, but E002-S01-004's `TimeStamper(fmt="iso", utc=True)` emits a `Z` designator. Both are explicit, but the family then has two spellings for one convention. Remediation: pick one and state it once (e.g. in `domain/`), and test both producers against it.

MINOR: [Core §3] E002-S01-001 gives `write_archive` two signatures: `write_archive(out, manifest: Manifest, members)` in the base contract and `write_archive(out, manifest_builder, members, *, cipher)` in the encryption seam. Remediation: keep only the final signature and say what `manifest_builder` is.

MINOR: [Core §3] E002-S01-001/E002-S01-002 list `VERIFY` and `PUBLISH` as separate `BackupStep`s, each emitting `phase_completed`, but verification actually runs inside `LocalArchiveStore.publish()` through the injected verifier. Remediation: say which step emits the `VERIFY` phase record and make sure verification is not run twice.

MINOR: [Core §1 single ownership] E002-S01-002 §Interface contracts (`cli/parser.py`) says "Epic 3 adds restore's own `--mode`". E002-S01-005 (decided 2026-10-20) registers the restore subparser's flags, including `--mode`, in E002, and the Epic List gives `cli/restore.py`, not `cli/parser.py`, to Epic 3. Remediation: change S01-002's sentence to point at S01-005.

MINOR: [Core §1; spec epics.md#epic-list] Epic 2's "Owns exclusively" cell lists `adapters/{tar,database,store,lock,config_writer}`. The stories also create `adapters/workspace.py`, `site_files.py`, `redaction.py`, `noop.py`, `clock.py` and `phase.py`, and only two of those are acknowledged in-story ("no other epic claims it"). Remediation: add them to the ownership cell so the parallel epics see them as Epic 2's.

MINOR: [Core §3; spec AD-1, Consistency/"never hand-written"] E002-S01-001 `domain/storage_target.py::parse_storage_target(text: str)` takes raw text, but `domain/` may not import `urllib`, and the story leaves open whether the adapter splits first. A dev given `text` in `domain/` will split on `"://"` by hand. Remediation: fix the signature to accept the `urlsplit` result (or its scheme and path), with the split done in `cli/`.

MINOR: [Core §6 stale docs] Several cross-references are stale:
- E002-S01-006 (`cli/credentials.py` "Bodies after Epic 5 … proposed story P3") and E002-S01-005 ("until P3 binds") — Epic 8 now owns those bodies (epics.md ownership note, 2026-10-27).
- E002-S01-002 still says its reclassification is "the orchestrator's", but the frontmatter is already `standard`.
- epics.md#epic-list "Sprint split" still says "2.4 stays" in sprint 2, which reorg R0002 reversed.

Remediation: update the references.

MINOR: [spec-conflict, AD-3] E002-S02-009 `WpConfigWriter` also implements `ConfigQuery.read_constants(config_file, keys)` with no restriction on keys. AD-3 allows the writer as the only exception "which writes and does not read for its own use", and E002-S01-001 asserts "nothing else opens `wp-config.php`". Remediation: limit `read_constants` to keys the descriptor does not carry, or to reading back the restore target's config, and say so in the docstring and in S01-001's single-reader statement.

MINOR: [spec-conflict, ADR-0015 §Decision] ADR-0015 says restore uses database's "`load` (with `replace=`)". E002-S02-007 and E002-S02-009 forbid `replace` at the port and in the adapter (staging per AD-18.3). The stories match the later spine decision. Remediation: amend ADR-0015's wording so the ADR does not contradict the guard test `tests/contracts/test_no_load_replace.py`.

MINOR: [Core §3, AD-14] E002-S02-007 `UnboundSearchReplace.relocate_content` raises a bare `RuntimeError`, which E002-S01-005 classes as "a non-`BackupError` exception reaching `main` is a defect". Remediation: either raise a named `BackupError` subclass, or put it on S01-005's documented programming-error allowance.

MINOR: [Core §3] E002-S01-001 `Manifest.source: dict[str, str | int | None]` is a mutable field on a frozen, slotted dataclass, so the Manifest is neither hashable nor deeply immutable. Remediation: use `Mapping` backed by `MappingProxyType` or a frozen `ManifestSource` dataclass.

## Previous findings

| AG | Status | Evidence |
|---|---|---|
| AG-1 | closed | epics.md#epic-list: Epic 2 owns `domain/*` "(including `domain/archive_format` and its v0 classifier)"; Epic 4 owns only "the v0 fixture Archives and their tests"; ownership note 2026-10-20. E002-S01-003 §Interface contracts restates it. |
| AG-2 | closed | epics.md#epic-list: Epic 2 owns `core/backup`, `cli/*`, `adapters/{tar,database,store,lock,config_writer}`; `cli/restore.py` moved to Epic 3 (2026-10-27). Residual unlisted adapters are a new MINOR above. |
| AG-3 | closed | epics.md#fr-coverage-map now names Epics 2–7 by their current titles (FR-14/27 → Epic 5, FR-22/23/24/30 → Epic 6, FR-11/12/13/29 → Epic 7). |
| AG-4 | closed | E002-S01-002 AC lines and §Interface contracts: `BackupScope.DATABASE = "database"`; one `cli/scope.py::CLI_MODE_TO_SCOPE`; "no Site Root members" AC restored; `test_no_db_spelling.py`. |
| AG-5 | closed | epics.md Epic List row and `## Epic 2` heading both read "Walking skeleton — take a backup and prove it". |
| AG-6 | closed | E002-S01-001 §Interface contracts `domain/archive_format.py` (first-member Manifest, roles `dump`/`site-root`/`config`, checksums over bytes as stored, no KDF params per ADR-0014); fixture-derived supported set in E002-S01-003 §Interface contracts "Supported format set". |
| AG-7 | closed | E002-S01-003 §Interface contracts: `ArchiveProbe` query port, pure `classify(MemberIndex) -> ArchiveView` in `domain/`, `Integrity` enum, `test_version_branching.py`. |
| AG-8 | closed | E002-S01-001 §Interface contracts `domain/site.py` (frozen, slots, no setters, `__post_init__` validation) and "One construction site" (`host:port` split once by database's `Connection.from_db_host`). |
| AG-9 | closed | E002-S02-008 §Interface contracts "Re-entrancy for the holder" plus `tests/unit/cli/test_lock_joining.py`; E002-S01-004 `test_backup_entry.py`. |
| AG-10 | closed | E002-S01-005 §Testability "Frozen CLI surface conformance" (19 spellings taken from the legacy parser by AST, `--only-appointed-in-asg` explained, console script, `python -m` strict-xfail) and the 2026-10-20 AD-25 literal 24-entry table with group-derived scope. |
| AG-11 | closed | E002-S01-004 §Interface contracts `cli/context.py` `RunContext`, `open_run`/`join_or_open`, `RunContextActiveError`, ContextVar-defined active Run. (The construction order of its adapters is the new MAJOR above.) |
| AG-12 | closed | E002-S02-007 §Interface contracts (Query/Command split, `adapters/noop.py`, `AdapterSet`) and §Testability `test_port_split.py`, `test_plan_equality.py`, `test_dry_run_gate.py`. |
| AG-13 | closed | E002-S01-001 §Interface contracts "No port that moves Archive or Dump bytes accepts or returns `bytes`" and §Testability "AD-21" (RSS bound at ≥10× and a walked `get_type_hints` check over `ports/`). |
| AG-14 | closed | E002-S01-006 §Interface contracts `adapters/redaction.py` (single implementation, contract test), the PyMySQL absence test and the walked `__repr__` check covering the Site Descriptor; FR-30 reuse obligation stated. (The `__str__` hook layering is the new MAJOR above.) |
| AG-15 | closed | E002-S01-004 §Interface contracts `configure_logging` (`merge_contextvars` first, UTC ISO timestamps, `ProcessorFormatter`), `--log-level` default INFO, `--log-format`, comparison with database's CLI. |
| AG-16 | closed | E002-S02-008 §Interface contracts "Released on signal" (SIGTERM → `RunTerminated`, `flock` released by the kernel) and `LockHolder(run_id, pid, started_at, hostname)`; refusal names the holder. |
| AG-17 | closed | E002-S01-003 AC and §Interface contracts `VerificationResult(passed, integrity, …)`; "a v1 Archive cannot pass as `STRUCTURAL_ONLY`". |
| AG-18 | closed | E002-S01-001 §Interface contracts `adapters/workspace.py` (`mkdtemp`, `0700`, `finally` removal, no `/tmp/database.sql`); folded into the Run Context by E002-S01-004. |
| AG-19 | closed | E002-S01-001 §Interface contracts `domain/errors.py` (one `BackupError` base, structured fields, `raise … from`) and §Testability `test_exception_hierarchy.py` (walked). |
| AG-20 | closed | E002-S02-007 §Interface contracts "Preflight port", corrected 2026-10-27 (value types in `domain/preflight.py`, Protocol in `ports/preflight.py`); `test_preflight_order.py`. |
| AG-21 | closed | E002-S01-001 §Interface contracts `adapters/store.py` (`O_EXCL` temp in the destination directory, required injected verifier, `os.replace` only on pass, existing final name refused); `test_local_store.py`. |
| AG-22 | closed | E002-S01-001 AC "with no Credential Provider given, the database credentials are resolved from that `wp-config.php` (FR-1)". |
| AG-23 | closed | E002-S01-004 §Interface contracts "Naming (AG-23)" and `test_no_wpconfigr_name.py`; epics.md story 2.5 no longer says `wpconfigr`. |
| AG-24 | closed | E002-S01-002 frontmatter `classification: standard`. |
| AG-25 | closed | E002-S01-005 §Interface contracts "Documentation generated, not hand-kept" (`tools/gen_cli_docs.py --check` in CI). |
| AG-26 | closed | E002-S01-005 §Interface contracts `domain/exit_codes.py` (0–5, 7 family; 6 reserved; 8 `VERIFICATION_FAILED`, 9 `LOCKED`; append-only; family conformance test derived from database's enum). (The mapping inconsistency is the new MAJOR above.) |
| AG-27 | closed | E002-S01-001 §Interface contracts `domain/storage_target.py` (`file://` and bare path → `file`, other schemes → `UnsupportedStorageSchemeError`). (The signature ambiguity is a new MINOR above.) |
| AG-28 | closed (original cause) / recurred | Reorgs R0001/R0002 moved the Run Context/workspace/logging, the exit codes and credentials into sprint 01, and ADR-0015 removed the transport dependency. The gap-pass additions to E002-S01-004 now reach forward into E002-S02-007/S02-008, and S01-005↔S01-006 form a cycle: see the sequencing MAJOR above. |
| AG-29 | closed | E002-S01-003 §Non-goals moves "verification before destructive restore" to Epic 3 story 3.1. E002-S02-007's ACs are restated on the backup path (no-op `ArchiveStore`, plan equality), and the restore half is assigned to Epic 3. |

## Per-principle walkthrough

- Core §1 Separation of concerns: findings on the `BackupError.__str__` layering, `l3io.wp.database` call-site ownership, `--mode` ownership, the unlisted adapters and the AD-3 reader. Otherwise PASS: the import-linter layers are respected (`ToolRequirement` correction, `MemberIndex`/`ArchiveView` in `domain/`).
- Core §2 Reuse: PASS. There is one extraction function, one redaction implementation, one CLI→domain scope map, one credential-source seam and one verifier shared by publish, verify and restore.
- Core §3 Design by contract: findings on Run Context construction order, the exit-code mapping, the Dry Run execution gap, `write_archive`, VERIFY/PUBLISH, the storage-target signature, `UnboundSearchReplace` and `Manifest.source`.
- Core §4 Testability: findings on the sprint/story dependencies and the strict-xfail gate. Otherwise strong: the guards derive their scope from the walked package, the parser or the enums, and include planted-violation cases.
- Core §5 Brevity: PASS (the AD-25 table is a deliberate literal, justified).
- Core §6 Comments/docs current: finding on stale cross-references.
- Core §7 Dependencies: finding on the stale database baseline. `platformdirs` was accepted with a recorded decision (S02-008). The redaction hand-roll is justified under the global rule's threshold.
- Core §8 GA over beta: PASS for E002 scope (`opentelemetry-exporter-otlp-json-file 0.65b0` is in the `otel` extra and Epic 6's).
- Core §9 Logging/correlation: PASS. There is one configuration site, `merge_contextvars` comes first, the Run ID is carried across database and config records, and caller correlation IDs are kept. A timestamp-format MINOR is noted.
- Core §10 Documentation: PASS for E002 (the CLI/exit-code README is generated, and the log schema is documented under `docs/`).
- Python overlay: PASS (uv, committed lock, `mypy --strict`, ruff, pytest; extras imported at function scope per AD-8).

## Gate

0 BLOCKER and 8 MAJOR. Under the step-04 policy, MAJORs block until each is resolved in the stories or recorded in an ADR. The 11 MINORs auto-defer to the backlog. Only the Run Context MAJOR option (b) needs an ADR. The rest are story edits.

## Recommended ADRs

- If the Run Context MAJOR is resolved by taking `AdapterSet` out of the Run Context: an ADR departing from `ARCHITECTURE-SPINE.md#ad-16--one-run-context-created-by-the-outermost-composition-root`.
- An amendment to ADR-0015 for the `load(replace=)` wording.

Sections read:
- `.claude/skills/l3io-arch-review/references/standards-core.md` (all), `standards-python.md` (all), `review-report.md` (all)
- `_bmad-output/implementation-artifacts/epic-002/arch/arch-gate-review.md` (all)
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-001.md` … `E002-S01-006.md` (all), `sprint-02/stories/E002-S02-007.md` … `E002-S02-009.md` (all)
- `ARCHITECTURE-SPINE.md` L44–110 (AD-1…AD-6), L111–161 (AD-7…AD-11), L162–180 (AD-12…AD-14), L208–213 (AD-16), L236–300 (AD-19…AD-26), L338–381 (Consistency Conventions, Stack), L458–467 (Deferred)
- `prd.md` L99–153 (§5.1), L154–200 (§5.2 FR-6…FR-8), L336–396 (§5.6)
- `epics.md` L124–203 (Epic List and ownership notes); grep hits for story headings, the FR Coverage Map (L110–119) and `wpconfigr`
- `docs/adr/0014-age-encryption-via-database.md` L1–60; `docs/adr/0015-credentials-delegated-to-database.md` (all)
- `pyproject.toml` L36–113 (dependency and pin grep), L302–355 (import-linter contracts)
- `_bmad-output/planning-artifacts/plan-2026-10-27-v1.yaml` L1–30, L88–140; `state/planned/epic-002/*/E002-*.yaml` `depends_on` blocks
