# Architecture drift review — E003 epic closure (Mode B)

Scope: `git diff a42ec0d..fea5a21 -- src docs` (29 files, +6512/-108), ADRs 0016, 0020-0023 (0016 and 0022 with their 2026-11-06 amendments), the six E003 stories, the sprint-01 and sprint-02 `arch-drift-review.md` files, and the architecture spine. Standards: core and Python overlay.

## Executive summary

The structural rules hold. `uv run lint-imports` keeps 3 of 3 contracts (110 files, 644 dependencies). `core/restore.py` imports only `domain/`, `core/` and the standard library (`posixpath`, `pathlib.PurePath`, `typing`, `dataclasses`). I found no SQL DDL, no `pymysql` and no `l3io.wp.database` type in a `cli/` signature. ADR-0016's and ADR-0022's 2026-11-06 amendments are implemented as decided: `archive_unauthenticated` is logged and printed (`cli/restore.py:1292`), and `docs/restore.md` carries the unauthenticated-Archive trust model and the Dump-privileges exposure. I did not reopen either.

One MAJOR was missed by both sprint reviews. The fix for sprint 02's SD-02-1 replaced module-name introspection with a boolean, `RestorePorts.performs_effects`, that the root sets to `not request.dry_run` and the executor branches on. That is a Dry Run conditional in `adapters/`. The other five findings are MINOR: a hole in the AD-4 gate, the God-module trigger sprint 01 set that has fired, a corrected premise for SD-02-2, an unenforced half of the ADR-0015 amendment, and an AD-4 outcome string that still names the Dry Run.

## Findings table

| # | Severity | Principle | Location | Finding | Remediation |
|----|----------|-----------|----------|---------|-------------|
| AD-1 | MAJOR | AD-4 / ADR-0022 option D | `ports/restore.py`, `adapters/restore_runner.py::_require_snapshot`, `cli/restore.py` | `performs_effects` is a Dry Run flag under another name, read by the executor | Drop the field; the executor decides from `result.performed`; the root refuses a not-performed snapshot in a real Run |
| AD-2 | MINOR | AD-4 gate reach | `ci.yml:103`, `adapters/phase.py`, `ports/adapter_set.py` | The gate pattern misses compound names such as `skipped_dry_run` | Widen the pattern; rename or exempt the outcome constant |
| AD-3 | MINOR | Core 1, 5 | `core/restore.py`, `cli/restore.py` | The SD-01-3 split trigger fired and was not acted on | Split along SD-01-3's seams before Epic 6 |
| AD-4 | MINOR | AD-16, AD-22 | `cli/restore.py::drive_restore` | The driver's home contradicts the layers contract | Record where the driver lives; add the contract |
| AD-5 | MINOR | ADR-0015 amendment | `adapters/staging.py`, `cli/adapter_sets.py` | The no-database-type-in-cli rule is not checked mechanically | Add a contract test |
| AD-6 | MINOR | Contract accuracy | `cli/restore.py::drive_restore` | The docstring says it never prints | Correct the docstring or route warnings through the logger |

## Findings

### AD-1 — MAJOR — AD-4 / ADR-0022 option C: `performs_effects` is a Dry Run flag under another name

- **Principle:** AD-4 ("no `if dry_run:` anywhere in `core/` or `adapters/`"); ADR-0022 rejects option C ("Dry Run checks branch on a dry-run flag"); core §1 and §3.
- **Location:** `ports/restore.py:34-43` (field `performs_effects`), bound at `cli/restore.py:1257` as `performs_effects=not request.dry_run`, read at `adapters/restore_runner.py:649-653` (`_require_snapshot`).
- **Evidence (checked in code):** `_require_snapshot` does `if not result.performed: if run.ports.performs_effects: raise SnapshotFailedError(...); return`. The field's own docstring says it is `False` "only when the bound command halves are the no-ops of a Dry Run". The root derives it from the Dry Run switch, not from the adapter that was bound. The executor therefore behaves differently in a Dry Run because of a flag, which is option C. Sprint 02's SD-02-1 was closed as `resolved-in-code`, and its remediation named `result.performed` as the input. This field is a second, separate input standing in for the Dry Run.
- **Why it is more than spelling:** the AD-4 gate is a `\b(dry_run|what_if)\b` grep (`ci.yml:103`) and cannot see this. A later check copying `if run.ports.performs_effects` makes a refusal vary with the flag, which is the "reports would-restore where the real Run would refuse" defect AD-4 names. It also weakens the AD-4 plan-equality guarantee: the executor is no longer byte-identical across the two adapter sets.
- **Remediation:** drop the field. Decide acceptance of a not-performed snapshot from the result alone: the no-op `SnapshotResult` carries `performed=False` and the planned `path`/`scope`, and the destroying phases (`CLEAN`, `EXTRACT`, `LOAD_DUMP`) take their own `performed` results as input. If a destroying phase can only run after a snapshot that was not performed because the adapters were no-ops, that phase's command is a no-op too. If the owner wants the boolean, record it as an ADR amendment to 0022 (option D plus a named, bounded exception) and add a contract test that `performs_effects` is read in exactly one place.
- **Owner-decision conflict:** none. ADR-0022 rejects the flag in terms. This is the code not meeting the ADR.

### AD-2 — MINOR — the AD-4 gate cannot see compound identifiers (rule reach, global rule 4)

- **Principle:** AD-4 enforcement; global rule 4 (a guard proves a rule, not its reach).
- **Location:** `.github/workflows/ci.yml:103` and its local mirror `tests/contracts/test_no_dry_run_outside_roots.py` (which reads the CI pattern, so both share the blind spot); live cases: `adapters/phase.py:54` (`SKIPPED_OUTCOME = "skipped_dry_run"`), `adapters/noop.py:10,13`, `adapters/staging.py:8`, `ports/restore.py:8`, `ports/adapter_set.py:9` (`dry_run_adapters`, `skipped_dry_run`).
- **Evidence:** `_` is a word character, so `\bdry_run\b` does not match `skipped_dry_run` or `dry_run_adapters`. The pattern also misses `DryRun`, `dryrun`, `whatIf` and `WhatIf`. Six lines outside `cli/` and `api/` name the Dry Run today and the gate is green. The `skipped_dry_run` value is a persisted log and phase outcome in `adapters/`, so it is a real name, not prose.
- **Remediation:** widen the pattern to `(dry[_-]?run|what[_-]?if)` case-insensitively with no word boundary. Then either rename the outcome to a mode-neutral value (for example `skipped_not_performed`, which matches ADR-0022's vocabulary; mind the downstream log consumers and `test_dry_run_gate.py`) or exempt that one constant by path in the gate with a comment. Plant a violation of each spelling when you widen it.

### AD-3 — MINOR — the sprint-01 God-module trigger has fired and was not acted on (SD-01-3)

- **Principle:** core §1 and §5.
- **Location:** `core/restore.py` (1495 lines, was 889 when SD-01-3 was filed), `cli/restore.py` (1691), `adapters/restore_runner.py` (865), `adapters/staging.py` (799).
- **Evidence:** SD-01-3 said to split `core/restore.py` "when E003-S02 touches the file". S02 did, and the file grew by 606 lines. It now holds the refusal errors, the target guard, snapshot and space rules, the plan builder, the undo derivation (`replaced_files`, `_load_undo`) and the resolution logic. `cli/restore.py` holds the request and flag types, the driver (`drive_restore`), identity confirmation, the extension seam and the exit rendering.
- **Remediation:** split `core/restore.py` along the seams SD-01-3 named (`restore_guard`, `restore_space`, `restore_undo`, `restore` for the plan). Do it before Epic 6 and Epic 7 add to either file. `import-linter` and `check_purity.py` already cover the new modules by package. Record it in the backlog if deferred again.

### AD-4 — MINOR — SD-02-2 is stronger than filed: the driver's home contradicts the layers contract

- **Principle:** AD-16 and the structural seed (the library root is `api/`); AD-22; ADR-0016.
- **Location:** `cli/restore.py:1024` (`drive_restore`) against `pyproject.toml:358-367` (`layers = ["…cli : …api", …]`).
- **Evidence:** sprint 02 recorded that "`lint-imports` has no contract forbidding `api` -> `cli`". It does have one: a colon between layers in `import-linter` makes them independent siblings, so `api` importing `cli.restore` fails the existing contract. `api/` is empty today, so nothing trips it. The docstring still says the driver is "the driver Epic 6's library root also calls". Epic 6 can therefore not call it without breaking the contract or moving it. No ADR records where the driver lives; sprint 02 recommended one and none exists (`grep drive_restore docs/adr` finds nothing).
- **Remediation:** before Epic 6, move the driver and the `RestoreRequest`/`RelocationFlags` value types out of `cli/` to a module both roots may import (an inward-layer module, or `api/` with `cli/` depending on it, which means changing the layers line). Write the ADR. Keep the `cli/` Dry Run exit rule in `cli/`.

### AD-5 — MINOR — ADR-0015's "no `l3io.wp.database` type in `cli/`" is not mechanically enforced and is literally breached by two type-only imports

- **Principle:** ADR-0015 amendment (2026-10-27); global rule 3.
- **Location:** `cli/adapter_sets.py:59-60` and `cli/credentials.py:27-28` (`if TYPE_CHECKING: from l3io.wp.database import CredentialSource`); `adapters/staging.py:206` (`from l3io.wp import database as wpdb`, outside the two named doors).
- **Evidence:** the two `cli/` imports predate E003 (not in the epic diff) and are annotation-only, but they are `l3io.wp.database` types named in `cli/`, which the amendment forbids. Sprint 02 passed the rule on the strength of a grep, not a test. `test_credential_sources_built_once.py` checks only constructor calls, not imports or annotations in `cli/` and `api/`. `staging.py` is E003's own new direct caller (SD-02-4, deferred). `tests/contracts/test_no_sql_ddl.py` does cover the "no DDL" half, and it passes.
- **Remediation:** type those two parameters with a backup-owned `Protocol` or alias defined in `domain/` or `ports/`, drop the imports, and add an `import-linter` `forbidden` contract (`cli` and `api` may not import `l3io.wp.database`) so the rule is checked over the derived package set. Move `_validate_name` into `adapters/database.py`.

### AD-6 — MINOR — contract text for `drive_restore` still overstates (SD-02-3 left open)

- **Principle:** core §3, global rule 3.
- **Location:** `cli/restore.py:1033-1043` against `:1292` (`archive_unauthenticated` also written to stderr) and the snapshot callback.
- **Evidence:** the ADR-0016 amendment of 2026-11-06 makes the stderr warning mandatory ("a safety warning is never only a log record"). That is the correct behaviour, but it makes the docstring's "never prints, except the AD-26 snapshot warning" false in a second place, and Epic 6 callers get stderr output the contract denies.
- **Remediation:** edit the docstring to list every stderr write, or pass a warning sink into `drive_restore` that the CLI binds to stderr and the library binds to the logger. The first is enough for the closure.

## Per-principle walkthrough

| Principle | Result |
|-----------|--------|
| AD-1 `core/` and `domain/` pure | PASS (`lint-imports` 3 kept; `core/restore.py` imports checked by hand) |
| AD-4 Dry Run is adapter selection | AD-1, AD-2 |
| AD-5 Restore Mode, no default | PASS (sprint 01 ran `check_mode_defaults.py`; not re-run) |
| AD-14 package-owned errors | PASS (`ArchiveChangedError`, `StagingCheckFailedError`, `ConfigCutoverError`, `CutoverTargetUnusableError` derive from `BackupError`/`RefusedError`) |
| AD-16 / AD-19 one Run Context, lock | AD-4 (driver placement) |
| AD-17 phase order as data | PASS (executor iterates `plan.phases`, `_dispatch` ends in `assert_never`) |
| AD-18 journal, staging, atomic config | PASS (`adapters/journal.py`, `adapters/staging.py` `_cutover`/`complete_cutover`, `adapters/config_writer.py`); ADR-0020 amendment `replaced_files` implemented in `core` and pure |
| ADR-0015 amendment | AD-5 (no DDL: PASS) |
| ADR-0016 amendment (unauthenticated Archive) | PASS, implemented and documented |
| ADR-0022 amendment (Dump privileges) | PASS, documented in `docs/restore.md`; no confinement, as decided |
| Core §1 separation | AD-3, AD-4 |
| Core §3 contract | AD-1, AD-6 |
| Core §5 brevity | AD-3 |
| Core §7 dependencies | PASS (`platformdirs`, `structlog` only; none preview) |
| Core §9 logging | PASS (structured events; no secret values in the paths read) |
| Core §10 documentation | PASS (`docs/restore.md` +401) |
| Python overlay | `lint-imports` PASS; mypy, ruff and pytest not re-run |

## Conflicts with owner decisions

None. ADR-0016 and ADR-0022 amendments, and `docs/closeout-gate.yaml` items BL-E003-035/036/037, are implemented and not reopened. AD-1 asks the code to meet ADR-0022's own rejection of option C.

## Already handled by the sprint reviews and not repeated

SD-01-1 and SD-02-1 (`resolved-in-code`); SD-01-2 and SD-01-4 (backlog); SD-02-5 and SD-02-6 (backlog).

Blocker: 0, Major: 1, Minor: 5

Sections read: ARCHITECTURE-SPINE.md#ad-4 L83-89; #ad-16 and #ad-17 L211-230; #ad-18 and #ad-19 L231-246; #ad-22 L259-268 (head); #ad-26 L300-305. The `spec-index.md` was read in full (lines 1-123) for pointers; PRD and epics ranges were not opened.
