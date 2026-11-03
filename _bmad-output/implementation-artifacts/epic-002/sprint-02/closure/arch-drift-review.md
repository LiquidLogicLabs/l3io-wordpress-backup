# Architecture drift review — E002 sprint 02

- **Mode:** l3io-arch-review Mode B (architectural review), spec alignment on
- **Scope:** `git diff cda0b46..dadafe1 -- src tools` (35 files, +4466/−210); stories E002-S02-007, -008, -009, -011, -012; ADRs 0001, 0016, 0018–0024 (by path, Decision sections)
- **Standards loaded:** `standards-core.md` §1–10, `standards-python.md`
- **Date:** 2026-11-03

## Executive summary

The sprint lands the backup Dry Run as adapter selection (AD-4), the Site Lock (AD-19), the restore vocabulary and frozen restore CLI surface (AD-5, AD-17, AD-22, AD-25), the restore ports with no-op twins and placeholders, and the real restore adapters. Layering mostly holds: `domain/` and `core/` stay pure, every database operation goes through `l3io-wp-database`, and the second `wp-config.php` write path is the one ADR-0023 accepted. The review found **0 BLOCKER, 2 MAJOR and 10 MINOR** findings. The most important fix is in the no-op restore twins: they hard-code a phase name for each command. Under Epic 3's executor, a Dry Run would emit `rewrite-config` and `clean` phase records inside `LOAD_DUMP`, out of AD-17's order and unlike the real Run.

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|---|---|---|---|---|---|
| SD-02-1 | MAJOR | AD-17 (phase order is data; cutover is the last step of `LOAD_DUMP`), AD-4 (core and executor identical under both sets), Core §1 | `src/l3io/wp/backup/adapters/noop.py` — `NoopConfigWriter._skipped` (`_skip(RestorePhase.REWRITE_CONFIG…)`), `NoopDatabase.revoke_grants` / `drop_database` (`_skip(RestorePhase.CLEAN…)`), `NoopDatabase.ensure_database` / `load_dump` (`LOAD_DUMP`) | Each no-op command hard-codes the restore phase it belongs to. `adapters/phase.py::join_phase` joins only when the innermost open phase has that exact name; otherwise it opens a new phase and emits its own `phase_completed`. The AD-17 amendments (2026-10-20, 2026-10-27) put the `DB_*` cutover `set_constants` call inside `LOAD_DUMP`. AD-18.3 drops the database that loses the cutover after the load. So under the dry-run set, a cutover emits a `rewrite-config` record nested in `load-dump`, and a drop emits a `clean` record after `load-dump`. The real adapters emit no phase records at all. A Dry Run's phase sequence would then differ from the real Run's, and it would contradict the order that Epic 3's phase-sequence test asserts ("no `DB_*` write precedes the end of the load"). The adapter is deciding plan structure that belongs to the executor. | Have the no-op twins mark the **currently open** phase as skipped, whatever its name, and open none of their own. Alternatively, have the executor pass the phase in. Add a contract test that drives each no-op command inside every phase it can legally be called from (e.g. `set_constants` inside `LOAD_DUMP`) and asserts no extra `phase_completed` record is emitted. |
| SD-02-2 | MAJOR | AD-22 (frozen CLI: "`python -m wpbackup2` as a working invocation") | `src/l3io/wp/backup/cli/parser.py` (module docstring claims it), `wpbackup2/__main__.py` (legacy tree), `tests/contracts/test_cli_surface.py` (strict xfail) | The parser docstring states that AD-22 preserves `python -m wpbackup2`. In practice the legacy `wpbackup2/` tree occupies that module name and is excluded from the wheel, so the invocation does not reach the new parser. The contract test carries a strict xfail. It is tracked only as BL-E002-086 ("no story owns it yet"). This hard-rule deviation of the frozen contract has no owner and no ADR. | Add the `wpbackup2/__main__.py` shim to `l3io.wp.backup.cli` in the wheel and decide whether to remove the legacy tree, or record an ADR accepting the gap until a named story. Either way, give BL-E002-086 an owning story before the restore driver (E003-S02-006) ships, and remove the xfail when it does. |
| SD-02-3 | MINOR | Core §10 (docs that contradict the code), spec alignment | `ARCHITECTURE-SPINE.md#ad-18` item 2; `epics.md` Story 2.9 AC; `stories/E002-S02-009.md` "Existing-library check" | ADR-0023 (owner decision, final) accepted a second `wp-config.php` write path, `replace_file`. AD-18.2 still says this package "delegates the write and its verification to `l3io-wp-config` rather than reimplementing either", with no amendment note. Story 2.9's AC says the same and pins `l3io-wordpress-database` v0.1.0, but the code calls v0.2.0 functions. The S02-009 story says "No … atomic-write code is written here". BL-E002-091 covers only the story claim. | Add an amendment line to AD-18.2 and to Story 2.9 that cites ADR-0023 and the v0.2.0 functions, the way AD-6 and AD-16 cite their ADRs. Correct the S02-009 note. |
| SD-02-4 | MINOR | Core §2 (reuse over copy-paste) | `adapters/config_writer.py::save_copy`, `::_write_over`; `adapters/lock.py::_write_record` (alongside `adapters/store.py`'s staged write) | The sequence mkstemp → write → flush → fsync → `replace` → unlink-on-failure → directory fsync is written out three times in this diff (twice in one module), plus once already in `store.py`. ADR-0023 accepts one extra write path, "one function behind one command port", not several copies. | Extract one `adapters/atomic.py` helper (temp beside target, optional owner/mode keep, optional read-back callback, directory fsync) and call it from all three sites. When BL-E002-091 lands the upstream whole-file replace, only that helper changes. |
| SD-02-5 | MINOR | AD-14 (structured fields, never dependency text), Core §3 | `adapters/database.py::_mapped` (`InvalidSiteDescriptorError(str(exc))`); `adapters/config_writer.py::_set_failure` (`exc.reason`), `_OWNER_MARKER` substring match, `set_constants` (`exc.reason` for `ConfigReadError`) | `_mapped`'s docstring promises "structured fields only, never the dependency's text", but one branch forwards `str(exc)`. `config_writer` passes the library's `reason` text through, and decides whether an error is an owner failure by matching a substring of the library's message, so an upstream rewording silently changes the classification. | Map these errors on type or structured fields only. Ask `l3io-wordpress-config` for a distinct owner-preservation error type, or a code field, and drop `_OWNER_MARKER`. Replace `str(exc)` with a fixed reason plus the identifier field. |
| SD-02-6 | MINOR | Core §1 (transport concern in an adapter), AD-22 (the Python API is not the CLI) | `adapters/database.py` `ADMIN_FLAGS = ("--admin-username", "--admin-password")`, `ADMIN_SOURCE`; `adapters/noop.py` imports them from the real adapter module | The adapter raises `AdminCredentialsRequiredError` naming CLI flag spellings, so a library (`api/`) caller is told to pass command-line flags. `noop.py` imports the real database adapter module just to get these constants. | Have the adapter raise a structured error naming the missing *credential* (administrator). Let `cli/exit.py` or `cli/credentials.py` render the flag names. Move the shared constant into `domain/` or `cli/`. |
| SD-02-7 | MINOR | Core §3 (contracts explicit), Core §2 | `cli/adapter_sets.py::_administrator_bound` (`getattr(command, "administrator_bound", False)`); `adapters/noop.py::NoopDatabase._require_admin` | `dry_run_adapters` reads an attribute that no Protocol declares, and `NoopDatabase` re-implements the real adapter's AD-29 refusal so the Dry Run refuses identically. The contract is implicit: a test name pins it, not a type. It is also duplicated across the real and no-op twins. | Declare `administrator_bound` on `DatabaseCommand`/`DatabaseQuery`, or carry it as a value on `AdapterSet`. Better, make the admin check a query (`check_ensure`/`check_drop_database` already require admin) that the executor calls before the command, so neither twin needs to own the refusal. |
| SD-02-8 | MINOR | Global rule 4 (derive guard scope from the source of truth), AD-5 enforcement reach | `tools/check_mode_defaults.py` `GUARDED_TYPES` | This sprint widened the AD-5 guard by hand to five type names. Any future closed choice added to `domain/restore.py` (another policy enum) is unguarded until someone remembers to edit the list, and the check still reports success. | Derive the set from `domain/restore.py` and `domain/backup_scope.py` (every `Enum` subclass plus `OverrideSet`, by AST or import). Plant a new enum in a test fixture to prove the scope follows. |
| SD-02-9 | MINOR | Core §1 (layer placement), AD-13 ("every external binary sits behind a port" in `adapters/`) | `ports/site_files.py::require_site_relative`; `cli/preflight.py::DumpReadinessPreflight` | A validation rule lives in `ports/`, which should hold only Protocols; it is pure and belongs in `domain/`. A `Preflight` implementation that calls `shutil.which` lives in the composition root and imports `adapters.database.blocked_dump_error`. It is not an `AdapterSet` field, so `backup()` constructs it implicitly. This is acknowledged as temporary until Epic 5 (decided 2026-10-28). | Move `require_site_relative` to `domain/` (re-export it if needed). When Epic 5 lands `adapters/preflight`, move `DumpReadinessPreflight` into `adapters/` and bind `Preflight` through the `AdapterSet`. Track that move against Epic 5 explicitly. |
| SD-02-10 | MINOR | AD-22 ("each correction is a named entry in the release notes"), Global rule 3 | `domain/flag_map.py` docstring; `stories/E002-S02-011.md` completion note 5 | The B-3 behaviour change (`--force` no longer means `ALLCLEAN`; `--cleanfirst` no longer removes the database) is recorded only in a docstring. There is no CHANGELOG and no backlog item, so the AD-22 release-notes obligation lives only in prose. | Create the release-notes source (e.g. `CHANGELOG.md` or a `release.yml` notes fragment) with this entry now, or open a tracked backlog item tied to the first release. |
| SD-02-11 | MINOR | AD-17 amendment 2026-10-27 ("recovery completes or reverts the set" of four `DB_*` keys), AD-18.1 | `domain/journal.py` `JOURNALED_CONSTANTS = {"DB_NAME","DB_HOST","DB_USER"}` | It is correct that `DB_PASSWORD` is never journaled (AD-7, ADR-0023). But nothing records how recovery reverts `DB_PASSWORD` after a cutover killed between its four writes. The journal cannot hold the prior value, and ADR-0020 does not mention it. | Record the mechanism in an ADR before Epic 3's cutover is built. Candidates: revert from the Safety Snapshot's config member, from the `--overwrite-wp-config` saved copy, or write `DB_PASSWORD` first so its revert is never needed. See Recommended ADRs. |
| SD-02-12 | MINOR | Core §10 (operational docs), Core §6 (no history in comments) | `docs/logging.md`; `adapters/database.py:695` ("review F1"), `adapters/phase.py:53` ("triage E-8"), `cli/parser.py:148` ("triage E-7") | `docs/logging.md` documents `planned_action` and `skipped_dry_run` but none of the sprint's other new events: `site_lock_acquired`, `stale_lock_reclaimed`, `site_locked`, `preflight_passed`, `config_written`, `site_root_cleaned`, `database_surveyed`, `database_ensured`, `dump_loaded`, `grants_revoked`, `database_dropped`. Nothing documents where the Site Lock lives or what an operator does on exit 9. Three comments cite review or triage IDs as change history. | Add the events and their fields to `docs/logging.md`, and add a short operational note on the lock directory (`platformdirs` user state `/locks`), stale-lock reclaim and `LOCKED`. Replace the review/triage citations with the invariant they protect. |

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | SD-02-1, SD-02-6, SD-02-9. `domain/`/`core/` purity holds: new `domain/` modules import only stdlib values; `core/backup.py` imports only `domain/`. |
| Core §2 Reuse over copy-paste | SD-02-4, SD-02-7. `blocked_dump_error` was correctly extracted and shared by `WpDatabase._refusal` and preflight. `read_defined` became the single `l3io.wp.config` reader. |
| Core §3 Design by contract | SD-02-5, SD-02-7. Total `FLAG_MAP` and `assert_never` matches are strong contracts; `JournalHeader.__post_init__` guards its keys. |
| Core §4 Testability | PASS. Real and no-op twins, injected lock directory, `ActionLog`, query/command split. The module-level lock registry is inherent to `flock` re-entrancy and is keyed per lock directory for test isolation. |
| Core §5 Brevity/readability | PASS. The 24-row literal `FLAG_MAP` is deliberate, so it can be reviewed row by row. |
| Core §6 Comments: current state, not history | SD-02-12 (review/triage citations). Otherwise, comments state invariants and cite ADRs. |
| Core §7 Dependency selection | PASS. `platformdirs>=4.3,<5` added (maintained, MIT, upper-bounded). |
| Core §8 GA over preview | PASS. No preview dependency was introduced. |
| Core §9 Structured, correlated logging | PASS. `run_id` and `dry_run` are bound by contextvars at the root; adapters use `structlog.get_logger`; only key names are logged, never values. |
| Core §10 Documentation | SD-02-3, SD-02-12. |
| Python: uv/lockfile/pyproject | Not in diff scope (`src tools` only), so not assessed. |
| Python: typing / ruff | PASS within scope. A module-wide `noqa: ARG002` in `cli/adapter_sets.py` is already tracked (BL-E002-087). |
| Python: structured logging, no `print` | PASS. Output goes through `cli/output.emit_result`. |

## Spec alignment (spine ADs touched by the diff)

| AD / ADR | Result |
|---|---|
| AD-1 / ADR-0001 | PASS (see §1 above). |
| AD-4 / ADR-0019 | PASS for backup: `dry_run_adapters` replaces only command halves, the selection happens once in `drive_backup`, and the CI grep is anchored to `cli/`/`api/`. For restore, see SD-02-1. |
| AD-5 | PASS on the rule; reach is in SD-02-8. |
| AD-13 | PASS. Preflight runs before the first command, and an unknown version fails a stated minimum. Placement is SD-02-9. |
| AD-14 | SD-02-5. |
| AD-16 / ADR-0016 | PASS. The lock is acquired in `open_run` before the workspace; joiners re-acquire only for a different Site; a Dry Run cannot join a real Run. |
| AD-17 | SD-02-1, SD-02-11. `RestorePhase` is an ordered enum and `is_subsequence` is pure. |
| AD-18.2/18.4 / ADR-0023 | PASS on behaviour (mode/owner kept, read-back, CRLF kept, `keep` applied in memory). Docs drift in SD-02-3; duplication in SD-02-4. |
| AD-19 | PASS. `flock` in the per-user state directory, refusal inside the Site Root, PID/start/host in the record, stale reclaim with a warning, re-entrant for one `run_id`. The known gaps BL-E002-081..083 are tracked. |
| AD-22 | SD-02-2, SD-02-10. `--whatIf` sits at top level, as in the legacy parser. |
| AD-25 | PASS. Total table; scope derived from the `restore mode` argument group; `OverrideSet` is a distinct type. |
| AD-28 / AD-29 | PASS. No SQL issued; grant escaping is the database package's; restore queries run as the administrator. |
| ADR-0018 | PASS. `DB_PASSWORD` is registered before use in `WpConfigWriter`; the no-op twin records keys only. |
| ADR-0020 / 0021 / 0022 / 0024 | PASS on the value types and port shapes (`phase_undo` in the header is permitted by ADR-0020; `performed` flags per ADR-0022; no decryption argument on `load_dump` per ADR-0024). |

## Gate

- BLOCKER + MAJOR (SD-02-1, SD-02-2): must be resolved, or recorded as an accepted ADR, before sign-off.
- MINOR (SD-02-3 … SD-02-12): defer to the backlog.

## Recommended ADRs

- **`DB_PASSWORD` revert at an interrupted cutover** (SD-02-11): how recovery restores the prior `DB_PASSWORD` when the journal may not hold it.
- **`python -m wpbackup2` and the legacy `wpbackup2/` tree** (SD-02-2), unless the fix lands directly.

## Assumptions

- I treat items that are tracked in the backlog without an ADR or owning story (SD-02-2) as unresolved for the BLOCKER/MAJOR gate.
- I have not proven SD-02-1 at runtime, because the restore executor is Epic 3's. It follows from `join_phase`'s name-equality rule and the AD-17/AD-18.3 placement of the cutover and drop.

Sections read:
- `_bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md` L44–386 (AD-1 … AD-30, Consistency Conventions, Stack; covers every range the `sections` command returned)
- `_bmad-output/planning-artifacts/epics.md` L371–389 (Story 2.9), L604–623 (Story 5.2)
- `_bmad-output/planning-artifacts/prds/prd-py-wordpress-backup-2026-09-12/prd.md` L154–222 (§5.2), L338–400 (§5.6), L487–501 (§7.1–7.2)
- Not opened: prd.md §5.3 L223–273, §5.4 L274–300 (relocation and credentials; no relocation code is in this diff)
- `docs/adr/0023` (full); `docs/adr/0001`, `0019`, `0022` (Decision sections); `0016`, `0020` (targeted reads)
- Stories E002-S02-007 (L141–176), -008 (L130–145), -009 (L100–121), -011 (L136–147), -012 (L128–139); `state/issues.yaml` BL-E002-077..091

Blocker: 0, Major: 2, Minor: 10
