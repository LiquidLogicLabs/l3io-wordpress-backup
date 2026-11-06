# Closure report: E005 (combined sprint and epic closure)

Goal: credentials from the environment and AWS Secrets Manager; fail before starting when a required tool is missing. Status: both stories done and merged; epic archived.

## Gates (main at 608ccc6 plus the closure fix)
ruff, format, mypy --strict (75 files), lint-imports (3 kept), check_purity, check_mode_defaults, gen_cli_docs --check, gen_tool_docs --check, uv lock --check: pass. pytest: 2717 passed, 88 skipped (no DB_HOST here; 2762 passed against MySQL 8.0 in the verified `make ci` at 608ccc6), with the E004-S01-002 v0 fixture tests deselected. `tests/contracts` (including the closeout gate) pass on the final tree except `test_v0_fixtures_reproducible`, which is E004-S01-002's blocked work.

## Reviews (one pass each over `git diff 8dbdb82..608ccc6`)
| Lens | Report | Result |
|---|---|---|
| clean-release + adversarial | adversarial-review.md | 0 critical, 0 high, 2 medium, 6 low |
| architecture drift | arch-drift-review.md | Blocker 0, Major 1, Minor 6 |
| red team (epic) | redteam-report.md | 0 critical, 0 high, 4 medium, 5 low, 4 observation |
| UX | skipped: no UI-facing story | |

## Fixed here
- Adversarial A-1 = red team M-1 (MEDIUM): `shutil.which` returned a relative path for an empty or `.` `PATH` entry, and the probe then ran it with `cwd` in the Run workspace. `preflight.py::_locate` now searches absolute `PATH` entries only. Regression test `test_the_default_lookup_ignores_relative_path_entries` fails on the old lookup (verified) and passes now. No integration test exercises this code (BinaryPreflight is not constructed by any production root).

## Deferred (owner of the code named)
- AD-1 MAJOR (no production caller): disposition `spec-proposal`, BL-E005-013; E008-S01-001 ACs now require a guard that fails while the three have no caller, plus README and docstring correction.
- Red team M-2 (wp-cli config in the ancestors of cwd), M-3 (timeout kills only the direct child), L-3, L-4, A-7: unreachable until E008 binds the preflight; added to E008-S01-001 ACs (four additions, 2026-11-06 note under "Checked by Epic 5's BinaryPreflight").
- Backlog (Low): BL-E005-006..012 (AD-3, AD-4/L-5, AD-5, AD-7, A-2, A-5, L-1). BL-E005-001..004 were already open from story review. 
- Outstanding issues from this closure: 1 Medium (BL-E005-005, owner decision below), 8 Low.

## Owner decisions
1. Red team M-4 (BL-E005-005): the redaction registry does not mask passwords of 3 characters or fewer and registers only after a successful resolve. Options: (a) refuse passwords under 4 characters at the credential boundary; (b) keep the warning. Recommendation (a), as an ADR-0018 amendment, owned by the redaction code.
2. Orchestrator decision to defer all wiring of `BinaryPreflight` to E008-S01-001 (arch AD-1): reported, not reversed. Recommendation: keep, now that the guard is an AC there.
3. S01-001 F-1 resolution (restore pins WordPress credentials to `wp-config.php`): behaviour fork between roots; guard test missing (BL-E005-009). Recommendation: keep and add the guard.

## Blocked stories
None.

## Spec changes
One disposition: AD-1 -> spec-proposal, `spec-proposals/AD-1.md`, BL-E005-013. No spec edit commits.

## Estimate vs actual
Actuals written by pm-status (see epic.yaml); tokens are exact from transcripts; orchestrator overhead of the caller is not included (its session transcript is not scoped to this epic).
