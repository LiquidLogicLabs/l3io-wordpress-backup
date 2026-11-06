# Architecture drift review — E003 sprint 02 (E003-S02-003..006, `wpbackup2 restore`)

Mode B. Scope: `git diff 4d85bd3..ae7a10d` over `src/` and `docs/` (`/tmp/s02-src.diff`, 15 source files, +3658/-70), ADRs 0001, 0015, 0016, 0018-0024, ARCHITECTURE-SPINE.

## Executive summary

The mechanical rules hold. `uv run lint-imports` keeps 3 of 3 contracts, `tools/check_purity.py` passes over 21 pure modules, and `tests/contracts` passes (430 passed, 6 skipped). `core/restore.py` imports only `domain/` and `core/`. `dry_run`/`what_if` is named only under `cli/`. No `l3io.wp.database` type reaches `RestoreRequest`/`RestoreOutcome`. One MAJOR finding: `adapters/restore_runner.py::_destroys` detects a Dry Run by inspecting adapter module names. That is a Dry Run conditional the AD-4 grep gate cannot see, and it contradicts the option ADR-0022 chose. The remaining findings are MINOR.

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|----|----------|-----------|----------|---------|-------------|
| SD-02-1 | MAJOR | AD-4 (spirit); ADR-0022 option D; Core §1, §3 | `adapters/restore_runner.py:629-635` (`_destroys`, `_DESTROYING_HALVES` :152), used at :649 | `_destroys` returns `type(getattr(adapters, name)).__module__ != noop.__name__`. Behaviour then depends on whether the bound adapters are the no-op set. That is `if dry_run` by another spelling, inside `adapters/`. The AD-4 grep gate (`\bdry_run\b`/`what_if`) cannot see it, and `getattr` on string names is not type-checked. ADR-0022 option D says checks take the effect's result (`performed`), not a condition. `_require_snapshot` already has `result.performed`; the `_destroys` branch decides whether `performed=False` is acceptable. | Make the check input-driven. Either carry a `performs_effects` fact on the command results the executor already receives, or have the composition root (`cli/restore.py`) pass a boolean-free value (for example the set of `RestorePhase`s whose commands are real) into `execute_restore`. Delete the module-name comparison. Add a test that plants a non-`noop` fake command adapter and a `noop`-named subclass, and asserts the verdict follows `performed`, not the module. |
| SD-02-2 | MINOR | Spine AD-16 / structural seed (`api/` is the importable root); Core §1 | `cli/restore.py:1024` (`drive_restore`), `:1-60` docstring | The "library root" driver (about 300 lines: lock, journal, descriptor, bind, probe, plan, executor, snapshot) lives in `cli/`, and its docstring says "the driver Epic 6's library root also calls". `api/` will have to import `cli.restore`, which inverts the intended `cli`/`api` symmetry. `lint-imports` has no contract forbidding `api` -> `cli`. `cli/backup.py` is a precedent, so this is drift, not a regression. | Record in an ADR (or an ADR-0016 amendment) that the driver is the shared root and where it will move in Epic 6 (a neutral `roots/` module that both `cli/` and `api/` import). Add an import-linter contract `api` must not import `cli` so the decision is enforced when `api/` is populated. |
| SD-02-3 | MINOR | Contract accuracy (Core §3, global rule 3) | `cli/restore.py:1033-1043` ("never prints") vs `:1187`, `:1293`, `:1360` | The docstring says `drive_restore` "returns or raises; it never prints, except the AD-26 snapshot warning". It also prints `STRUCTURAL_ONLY_WARNING` to stderr (`:1187`), and the snapshot callback prints to stderr (`:1293`). A library caller (Epic 6) therefore gets stderr output the contract denies. | Either route both warnings through the logger and have `run_restore` print them from `RestoreOutcome` fields, or correct the docstring to list all prints. Prefer the former, given the stated library use. |
| SD-02-4 | MINOR | ADR-0015 amendment / E002-S01-001 ("one place that calls l3io-wp-database") | `adapters/staging.py:195-200` (`_validate_name`) | `staging.py` imports `l3io.wp.database` directly. Only `adapters/database.py` and `adapters/crypto.py` (older precedent) are named or established as callers. It is not credential construction, so the amendment's text is not breached, but the "single door" erodes. | Move the `validate_database_name` call into `adapters/database.py` and have `staging.py` use that wrapper (it already imports the module for other things). |
| SD-02-5 | MINOR | Core §1 / AD-14 (public boundary); ADR-0015 amendment intent | `cli/restore.py:699-740` (`RestoreRequest`) | The request, which Epic 6 exposes as the public restore counterpart, carries `AdminTls` (an `adapters.database` type) and `RelocationFlags`, and the module exposes the mutable global seam `RESTORE_EXTENSION` (`:663`, read at `:1052`). No database-package type crosses (rule met), but an adapters type does. | Move `AdminTls` to `domain/` (it holds only a mode and a CA path). Replace the module global with a parameter or a registered port, so tests and Epic 7 do not monkey-patch a global. |
| SD-02-6 | MINOR | Spine port list (`Clock`); Core §4 testability | `adapters/staging.py:373,410` (`time.monotonic_ns`) | Durations are read from the wall clock directly, not through the spine's `Clock` port, so the load duration reported in FR-9's report is not deterministic in tests. | Inject the existing clock/timer port into `load_staged`, or document `monotonic_ns` as intentionally unported in the ADR-0022 consequences. |

## Per-principle walkthrough

Core: §1 Separation of Concerns: SD-02-1, SD-02-2, SD-02-5. §2 Reuse: PASS (`--archive-database` reuses `Connection.from_db_host` through `database_location_from_db_host`; the snapshot reuses `backup()`). §3 Design by Contract: SD-02-1, SD-02-3. §4 Testability: SD-02-6. §5 Brevity: PASS (`cli/restore.py` is 1440 lines; `drive_restore` is long but linear and step-commented). §6 Comments (current state, not history): PASS (bug-ID references only). §7 Dependency selection: PASS (`platformdirs`, `structlog`, `json`, no new hand-rolled parsers; the journal serialises through `json`, the archive-database split is a two-partition syntax check that delegates the host parse). §8 GA over beta: PASS (no new dependencies). §9 Logging: PASS (structlog events `restore_mode_resolved`, `archive_unauthenticated`, `safety_snapshot_*`; secrets registered in the Run's registry). §10 Documentation: PASS (`docs/restore.md`, ADR-0016/0020/0022 edits present).

Python overlay: uv/poetry: PASS. Version and runtime: PASS. Toolchain (mypy strict, ruff, import-linter): PASS for `lint-imports`; mypy and ruff were not re-run in this review.

Rules named in the task:
- AD-1: PASS (`lint-imports` 3 kept; `check_purity.py` passes; `core/restore.py` imports are `posixpath`, `collections.abc`, `dataclasses`, `pathlib.Purepath`, `typing`, and package `core`/`domain` only).
- AD-4 (grep gate): PASS (the `tests/contracts` no-dry-run test passes; matches are only in `cli/`). Semantic leak: SD-02-1.
- ADR-0015 amendment: PASS in `cli/` (the only `l3io.wp.database` mentions are `TYPE_CHECKING` imports in the older `cli/credentials.py` and `cli/adapter_sets.py`, outside this diff, and a docstring in `cli/restore.py`). See SD-02-4 and SD-02-5 for edges.
- Ports/adapters layering: PASS (nothing under `adapters/`, `ports/`, `core/`, `domain/` imports `cli`/`api`).

## Gate

BLOCKER + MAJOR (SD-02-1) must be fixed or ADR-justified before sign-off. MINOR (SD-02-2..6) defer to the backlog.

## Recommended ADRs

- Where the shared restore driver lives (`cli/` today, Epic 6 `api/` tomorrow), plus an enforcing import-linter contract (SD-02-2).
- Amend ADR-0022 to state how the executor decides whether a not-performed snapshot is acceptable, without module-name introspection (SD-02-1).

Sections read: ARCHITECTURE-SPINE#ad-4, #ad-16, #structural-seed (L83-89, L211-218, L387-432); the other sections listed by `spec-align sections` were not opened (code-level evidence was sufficient).

Blocker: 0, Major: 1, Minor: 5
