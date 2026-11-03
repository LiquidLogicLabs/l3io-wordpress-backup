# Architecture drift review — epic E002 (cross-sprint)

- **Mode:** l3io-arch-review Mode B, spec alignment on
- **Scope:** `git diff 3e9f80a..d30367c -- src` (58 files, +10737/−42); ADRs 0014–0024 and the earlier ADRs they cite; spine ADs 1, 4, 6, 8, 16–19, 21, 22, 24, 26
- **Not re-raised:** SD-01-1..6, SD-02-1..12 (already dispositioned), BL-E002-086, BL-E004-042, and the owner decisions in the brief
- **Standards loaded:** `standards-core.md` §1–10, `standards-python.md`
- **Date:** 2026-11-03

## Executive summary

`uv run lint-imports` passes: 3 contracts kept, 0 broken, 103 files, 510 dependencies. `domain/` and `core/` import nothing from `ports/`, `adapters/`, `cli/` or `api/`. No `adapters/` module imports `cli/`. No `if dry_run` branch exists outside `cli/` (a grep over `adapters/`, `ports/`, `core/`, `domain/` found only docstrings and the `skipped_dry_run` log literal). Restore phase order is data (`domain/restore.py`: `RESTORE_PHASE_ORDER`, `is_subsequence`), and ADR-0014/0020/0021/0022/0023/0024 are consistent with the code in the places I checked. I found **0 BLOCKER, 1 MAJOR and 3 MINOR** cross-sprint drifts. The MAJOR is that the two sprints name `phase` values in incompatible ways, in a field that `docs/logging.md` presents as one operator schema.

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|---|---|---|---|---|---|
| AD-1 | MAJOR | Spine AD-17 / AD-24 (one phase vocabulary; spans and logs follow the phases); Core §9 (one correlated schema); Core §10 | `src/l3io/wp/backup/core/backup.py:33-34` (`BackupStep.DUMP = "DUMP"`, ...); `src/l3io/wp/backup/domain/restore.py:130-137` (`RestorePhase.LOAD_DUMP = "load-dump"`, `REWRITE_CONFIG = "rewrite-config"`); `src/l3io/wp/backup/adapters/noop.py:365,391,538`; `docs/logging.md:53` | Sprint 1 emitted the `phase` field in upper case (`DUMP`, `VERIFY`). Sprint 2 declared restore phases in lower-case kebab (`load-dump`, `rewrite-config`, `search-replace`), and the no-op twins already emit them as `phase` through `_skip(RestorePhase.X.value)`. `docs/logging.md:53` defines the field as "The step: `DUMP`, `COLLECT_SITE_ROOT`, ... `PUBLISH`" with a closed list, and `jq 'select(.event=="phase_completed")'` (logging.md:10) is the documented query. Epic 3's executor will therefore emit a second spelling in the same field, and `VERIFY` exists in both flows with different spellings (`VERIFY` from `BackupStep`, `verify` from `RestorePhase`). The spine writes AD-17's phases in upper case, so the lower-case values are not in the spec. Nothing is wrong yet, but the field is still unemitted by a real restore, so renaming now costs a docstring and a test; after Epic 3 it is a log-schema break. | Pick one spelling for the `phase` field. The smallest change is to make `RestorePhase` values the enum names (`LOAD_DUMP`, ...) to match AD-17 and `BackupStep`; or record an ADR that fixes the kebab form and migrate `BackupStep`. Extend `docs/logging.md`'s schema table to list the restore phases and add a contract test that every `BackupStep` and `RestorePhase` value matches one regex, so a third spelling fails CI. |
| AD-2 | MINOR | Global rule 4 (a guard's reach); Spine AD-4 enforcement | `.github/workflows/ci.yml:103` (`grep -rnE '\b(dry_run|what_if)\b'`) | The AD-4 gate matches whole words only. I ran the pattern against `self._dry_run`, `isDryRun` and `whatIf`: none matches; only `self.dry_run` does. An `if self._dry_run:` or `if is_dry_run:` in `adapters/` or `core/` passes CI, which is exactly the defect class AD-4 names. The sprint-2 review (SD-02-8) covered the AD-5 guard's scope, not this one. Today the tree is clean (see summary), so this is reach, not a live violation. | Match case-insensitively on the stems `dry_?run` and `what_?if` (`grep -riE 'dry_?run|what_?if'`), outside `cli/` and `api/`, and allowlist the single `skipped_dry_run` literal and docstring prose by an explicit constant. Plant `self._dry_run` and `whatIf` in a test fixture to prove the gate fails. |
| AD-3 | MINOR | Spine AD-1 / AD-4 (cli is a composition root); Core §1; Global rule 4 | `src/l3io/wp/backup/cli/backup.py:183-208` (`_SkipStaged`, an `ExclusionPolicy`), `:377-` (`execute_backup_plan`), `:243-342` (`plan_backup`, `plan_actions`); `src/l3io/wp/backup/api/__init__.py` (empty); `pyproject.toml:357` (`"l3io.wp.backup.cli : l3io.wp.backup.api"`) | The plan interpreter, the step-execution loop and an exclusion policy sit in `cli/` (2860 lines in total), and `api/` is empty. Epic 6 means to reach these through `cli/` functions (`epics.md:173`). I verified with a throwaway package that `a : b` siblings may import each other under import-linter 2.15, so `api` importing `cli` is not a violation. Two consequences: the AD-4 grep gate exempts all of `cli/`, so an effect-level `dry_run` conditional in `execute_backup_plan` would pass; and the layer contract does not stop `cli` importing `api` back, so the "composition root" boundary is by convention. This is a plan that was chosen (Epic 8 note), so I rate it MINOR. | Move `_SkipStaged` into `adapters/` (it is a policy implementation) and `execute_backup_plan` behind a module `api` can own, or add a contract that `l3io.wp.backup.api` may import `cli` but not the reverse. Narrow the AD-4 exemption to `cli/__init__.py`, `cli/adapter_sets.py`, `cli/context.py` and the `drive_*` functions, or add the first `dry_run` use outside `drive_backup` as a failing case. |
| AD-4 | MINOR | Core §7 (dependency selection); ADR-0014 / ADR-0024 ("no `cryptography` cipher"); AD-8 | `pyproject.toml:37` (`"cryptography>=45,<51"` in `[project].dependencies`); `tools/check_purity.py:65`; `pyproject.toml:325` | `cryptography` is a mandatory runtime dependency of every install, but nothing under `src/` imports it (grep: no hit). Encryption is delegated to `l3io-wordpress-database` (ADR-0014, ADR-0024; `adapters/crypto.py` imports only `l3io.wp.database.encryption`), and ADR-0006's `cryptography` design was never accepted. The import-linter contract lists it as forbidden in `domain/` and `core/`, which is a guard over an import that no longer has a reason to exist. A native wheel with its own CVE stream ships to every cron host for no use. | Drop `cryptography` from `dependencies` (keep it in the forbidden list so the intent stays enforced), or record in an ADR why it stays. Add a check that every base dependency is imported somewhere under `src/`. |

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | AD-3. Layering holds in the import graph. |
| Core §2 Reuse over copy-paste | No new cross-sprint finding. The atomic-write duplication is SD-02-4. |
| Core §3 Design by contract | PASS. `RestorePhase` order is data and `is_subsequence` is pure; the port query/command split is used by both sprints' adapters. |
| Core §4 Testability | PASS. No-op twins and injected clock/lock directory are shared across both sprints. |
| Core §5 Brevity | PASS. |
| Core §6 Comments as current state | No new finding beyond SD-02-12. |
| Core §7 Dependency selection | AD-4. `platformdirs` adoption is documented. |
| Core §8 GA over preview | PASS. No preview dependency; family packages are bounded ranges. |
| Core §9 Logging | AD-1. `run_id` and `dry_run` bound once at the root in both sprints' paths. |
| Core §10 Documentation | AD-1 (logging schema). |
| Python: uv / lockfile | Not assessed beyond the dependency line in AD-4. |
| Python: typing / ruff | PASS within scope. |

## Spec alignment (cross-sprint)

| AD / ADR | Result |
|---|---|
| AD-1 / ADR-0001 | PASS. `lint-imports`: 3 kept, 0 broken. `core/` imports only `domain/`. |
| AD-4 / ADR-0019 | PASS on code (selection once at `cli/backup.py:638`; no conditional elsewhere). Gate reach is AD-2 and AD-3. |
| AD-6 / ADR-0017 / ADR-0024 | PASS. One payload member; `adapters/tar.py` and `core/verify.py` import format constants from `domain/archive_format` (the proxy branch is SD-01-2, dispositioned). |
| AD-8 | PASS. `adapters/crypto.py` defers the database import into each method; no module-scope optional import found. |
| AD-16 / ADR-0016 | PASS. Backup (sprint 1) and the lock (sprint 2) use the same `open_run`/`join_or_open`. |
| AD-17 / ADR-0022 | PASS on order and type. `RESTORE_PHASE_ORDER = tuple(RestorePhase)`; naming is AD-1 above. |
| AD-18 / ADR-0020/0021/0023 | PASS on the shapes in `domain/journal.py` and `adapters/config_writer.py`; open items are SD-02-11 and BL-E002-091. |
| AD-21 | PASS. No whole-archive `read()` in `adapters/` or `domain/`; the only `handle.read()` calls are `wp-config.php` reads in `adapters/config_writer.py:310,368`. |
| AD-22 | The `python -m wpbackup2` gap is BL-E002-086 (not re-raised). |
| AD-24 | The span attribute names in `cli/context.py:230` are `l3io.operation` and `l3io.dry_run`, not the spine's `wpbackup2.phase` / `dry_run`. The tracing adapter is Epic 6's, so I did not raise it. Record the rename when `adapters/tracing` lands. |
| AD-26 / ADR-0024 | PASS. `PassphraseCipher` is the only key path; `backup` binds it by `cipher=None` until E004-S01-003. |

## Gate

- BLOCKER and MAJOR: AD-1 must be resolved or ADR-justified before epic sign-off.
- MINOR (AD-2, AD-3, AD-4): defer to the backlog.

## Recommended ADRs

- The `phase` field's spelling across backup and restore (AD-1), unless the enum values are simply changed.

## Assumptions

- I did not re-run the test suite. Findings rest on `lint-imports`, grep and reading.
- AD-1 is a log-schema drift, not a runtime bug: no real restore executor exists yet, so only the no-op twins emit the kebab spelling today.

Blocker/Major/Minor: 0/1/3

Sections read:
- `ARCHITECTURE-SPINE.md` AD-1, AD-4, AD-6, AD-8, AD-16, AD-17, AD-18, AD-19, AD-20, AD-21, AD-22, AD-24, AD-26, Structural Seed
- `docs/adr/0014`, `0024` (Decision/Context); `0011`, `0016`, `0019`–`0023` by header and targeted grep
- `epics.md` L165–176
- Sprint 01 and 02 `arch-drift-review.md` and both `drift-dispositions.yaml`
- Source: `core/backup.py`, `domain/restore.py`, `adapters/crypto.py`, `adapters/noop.py` (grep), `cli/backup.py` (L55–80, 183–210, 377–400, 516–580), `cli/verify.py` (imports), `api/__init__.py`, `pyproject.toml` L28–50 and L315–362, `.github/workflows/ci.yml` L95–115, `docs/logging.md` L40–58
- Not opened: PRD, stories, `arch-gate-review` records under `epic-002/arch/`, the test suite
