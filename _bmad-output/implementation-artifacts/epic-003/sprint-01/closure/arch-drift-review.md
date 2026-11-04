# Architecture drift review — E003-S01 (Mode B)

Scope: stories E003-S01-001 and E003-S01-002; `git diff a42ec0d..10193bf -- src docs tests tools`; ADR 0020-0023.
Standards: core §1-10, Python overlay. Spec alignment on (spine AD-1, AD-4, AD-5, AD-17, AD-25, AD-11, AD-14).

## Executive summary

The sprint holds the spine's structural rules. I ran the import-linter, purity, mode-default, mypy and ruff checks and the whole contracts suite, and all passed. No BLOCKER. There is one MAJOR: the restore executor writes the Archive's `wp-config.php` through raw `os.open`, outside every port, so a Dry Run with `--overwrite-wp-config` still writes a credential-bearing file. The other three findings are MINOR. The first action is to route that write through a command port (SD-01-1).

## Findings table

| # | Severity | Principle | Location | Finding | Remediation |
|---|----------|-----------|----------|---------|-------------|
| SD-01-1 | MAJOR | AD-4 (Dry Run is adapter selection; only writes are suppressed), AD-1 (effects through a `ports/` Protocol), Core §4 | `src/l3io/wp/backup/adapters/restore_runner.py:499-520` (`_archive_config`, called from `_rewrite_config`) | `_archive_config` writes the Archive's config into the Run workspace with `os.open`/`os.fdopen` directly, not through a command port. ADR-0022 has the executor run every phase in a Dry Run over no-op command halves, so this write is not suppressed. The Dry Run therefore creates a `0600` file holding the source site's DB credentials. The executor's own docstring says a Dry Run is the no-op command halves. This is the defect class AD-4 exists to prevent. The CI grep gate and `test_restore_plan_equality.py` cannot see it, because the write is neither a conditional nor part of the plan. | Add a `write_workspace_file` (or similar) method to the command half of a port, with a no-op in `adapters/noop.py`. Alternatively pass the config body to `config_command.replace_file(config_file, body, keep=...)` so no intermediate file exists. Add a recording-harness test: a Dry Run with `OVERWRITE` leaves the workspace without the file. |
| SD-01-2 | MINOR | AD-1 (core computes decisions, adapters perform effects), Core §4 | `adapters/restore_runner.py:360-405` (`_check_space`), `:456-496` (`_extract` exclusion and `wp_config_kept` choice) | The adapter-side executor assembles the space-need inputs: the v1/v0 size source branch, the `archive.stat()` call and the payload sizing. It also decides which config to exclude (`keeps`, `laid_in_root`). These are decisions over facts, in the layer meant to hold effects. They are testable only through the executor and a real filesystem (`Path.stat`). ADR-0022 puts the executor in `adapters/`, but it does not put these decisions there. | Move the size-source selection and the exclusion predicate into `core/restore.py` as pure functions, taking the compressed size as an input. Leave `stat` and `member_digests` as the only effect calls in the executor. |
| SD-01-3 | MINOR | Core §1 (God module), Core §5 | `src/l3io/wp/backup/core/restore.py` (889 lines) | One module carries five concerns: the refusal error types, the target guard, snapshot scope/location, space/expansion and plan building. The imports are clean, but the module is heavy for a reader to navigate. | Split into `core/restore_guard.py` (errors, `check_target`, `is_orderly_failure`), `core/restore_space.py` and `core/restore.py` (plan). Do it when E003-S02 touches the file, not as a separate change. |
| SD-01-4 | MINOR | AD-11 (an archive is untrusted input), Core §3 | `adapters/restore_runner.py:360-381` with `PREFLIGHT` ordered before `VERIFY` | `_check_space` sizes the destinations from the unverified Manifest, because `PREFLIGHT` precedes `VERIFY`. Extraction enforces the indexed sizes and a running cap (`tar.py`, BL-E002-116), so a forged Manifest cannot write more than it declares. But an attacker-controlled Manifest can understate size and pass the pre-check. The check is an advisory early refusal. The code and `docs/restore.md` do not say so. | Add one sentence to the `_check_space` docstring and to `docs/restore.md` saying the pre-check trusts the index and extraction is the enforcement. No code change. |

## Checks run (import and purity)

- `lint-imports`: AD-1 "domain/ and core/ import no I/O module", "the pure layers never face outward" and "layers run inward only" all KEPT (106 files, 556 dependencies). The new `ports/restore.py` imports only `domain` and `ports`. `core/restore.py` imports `domain/*` and `core.backup` only. `adapters/restore_runner.py` imports `core`, `domain`, `ports` and `adapters`, which is inward.
- AD-4: the CI grep `\b(dry_run|what_if)\b` outside `cli/` and `api/` is clean. `test_no_dry_run_outside_roots.py` and `test_dry_run_gate.py` pass. `restore_runner.py` and `core/restore.py` name the Dry Run only as prose ("Dry Run"), never as an identifier. The residual AD-4 gap is SD-01-1, which the grep cannot see.
- Purity: `tools/check_purity.py` passed over 21 pure modules. `tools/check_mode_defaults.py` (AD-5, no default on a `RestoreMode`) passed over 67 modules. `tests/contracts/test_no_flag_reads.py` (AD-25) is new, with its scope derived by walking the package.
- `mypy src` is clean, `ruff check src tests` is clean, and `pytest tests/contracts` gives 411 passed, 6 skipped. The skips have stated reasons: no CI database, and `ci.yml` excluded from the act legs.
- `src/wpbackup2` (AD-22) is a thin shim over `l3io.wp.backup.cli:run_from_cli`, and the packaging comment in `pyproject.toml` records it.

## Per-principle walkthrough

| Principle | Result |
|-----------|--------|
| Core §1 Separation of concerns | SD-01-2, SD-01-3 (layering itself PASS) |
| Core §2 Reuse | PASS (`verify_archive_view` gained a `phase_name` parameter instead of a copy; `require_patched_tarfile` is one shared helper) |
| Core §3 Design by contract | SD-01-4 (the others PASS: `ViewMember.size` and `backup_scope` are explicit and Optional for v0; the executor refuses a header-mode mismatch with a `ValueError` before `PREFLIGHT`) |
| Core §4 Testability | SD-01-1, SD-01-2 (the recording harness is plain pytest helpers, not a runner) |
| Core §5 Brevity/readability | SD-01-3 |
| Core §6 Comments | PASS (comments give intent and cite ADRs/BLs; none narrate history) |
| Core §7 Dependency selection | PASS (no dependency added in this diff) |
| Core §8 GA over preview | PASS (no preview dependency; tarfile is gated by capability) |
| Core §9 Logging | PASS (structlog, structured `wp_config_kept` and `wp_config_overwritten` events, phase records through `phase()`; no secrets logged, and `RestorePorts.__repr__` hides the credential callable) |
| Core §10 Documentation | PASS (`docs/restore.md` added; ADRs 0020-0023 recorded) |
| Python overlay (uv, lockfile, typing, ruff, structlog, pytest) | PASS |
| AD-17 phase order as data | PASS (the executor iterates `plan.phases`; `_dispatch` ends in `assert_never`) |
| AD-14 package-owned errors | PASS (`ArchiveChangedError`, `TargetConfigMissingError` and the core refusals derive from `BackupError`/`RefusedError`) |

## Conflicts with owner decisions

None found. ADR-0022 (executor in `adapters/`, every phase runs in a Dry Run) and ADR-0023 (config kept unless overwrite) are implemented as written. SD-01-1 asks the code to meet ADR-0022's own premise that a Dry Run suppresses writes. It does not reopen the ADR.

## Gate

BLOCKER + MAJOR: SD-01-1 must be resolved or ADR-justified. MINOR: defer to backlog.

## Recommended ADRs

None. If SD-01-2 is deferred, add a line to ADR-0022 saying which decisions the executor owns.

Sections read: ARCHITECTURE-SPINE.md#ad-1 L44-51; #ad-4 L83-89 and rule text to L96; #ad-5 L90-96; #ad-17 L219-230; ADR-0020..0023 (heads and Decision openings only). The spec-align `sections` output was listed and not otherwise opened. Not opened: AD-7, AD-10, AD-11, AD-14, AD-18, AD-23, AD-25, AD-29, PRD §5.2/§5.3/§5.6, and the epics, so findings that rest on them are checked only against the code and the ADRs.

Blocker: 0, Major: 1, Minor: 3
