# E008 closure report (combined sprint 01 and epic closure, 2026-11-07)

Goal: bind every epic's providers into the composition root. Story E008-S01-001 (14029e8) done. Lenses run
once: adversarial (`adversarial-review.md`), red team (`redteam-report.md`), arch drift
(`arch-drift-review.md`, Blocker 0, Major 3, Minor 5), retrospective (`retrospective.md`). Sprint
`closure/` points here (`sprint-01/closure/closure-report.md`).

## Fixed in this closure (not committed)

| Finding | Fix | Regression test (fails without the fix) | Integration-tested code touched |
|---|---|---|---|
| BL-E008-002 / R-04 | `cli/preflight.binary_preflight` probes from `/` (`PROBE_DIRECTORY`), not the Run workspace under `$TMPDIR`; `docs/restore.md` and the adapter docstring updated | `test_a_wp_cli_config_planted_beside_the_workspace_cannot_deny_the_probe` (verified failing with the old line); `test_diagnose_gets_a_binary_preflight_probing_from_the_filesystem_root` | Yes: `binary_preflight` feeds every root; `tests/integration/test_preflight_refusal.py`, `test_wp_cli_missing.py` exercise it |
| A-02 (accepted-and-ignored part) | `backup --list-default-excludes` plus any other backup flag is a `UsageError` (exit 2), flags derived from the backup parser | `test_list_default_excludes_refuses_every_other_backup_flag` (6 cases, verified failing without the fix) | No |
| AD-1 | ADR-0018 amendment recording `Secret` (owner decision, status line untouched, nothing marked Accepted by me); stale BL-E006-009 sentence fixed | docs only | No |
| AD-3 | `docs/api.md` prose about `context=` rewritten; new derived guard `stale_keywords` in `tests/contracts/test_api_docs.py` | `test_a_removed_keyword_left_in_prose_is_caught` (plants `context=`) | No |
| AD-7 | ADR-0024 first amendment paragraph no longer lists the Safety Snapshot as bound by the floor | docs only | No |

Gates run on the final tree: ruff check and format, `mypy --strict src`, lint-imports (4 kept), `check_purity`,
`check_mode_defaults`, `gen_cli_docs --check`, `gen_tool_docs --check`, `uv lock --check`; pytest results below.
No new error class; `pyproject.toml` and `uv.lock` untouched.

## Owner decisions (batched)

1. **Manifest cap timing (BL-E008-003, AD-2, MAJOR, disposition spec-proposal, BL-E008-010).** Refused at
   `WRITE_ARCHIVE`, after the dump; a Dry Run reports success. Options: (a) amend the AC and ADR-0019 to say so;
   (b) a read-only measuring query before `DUMP`, run in Dry Runs too (a second Site walk, an ADR-0019 exception).
   Recommendation: (a) for v1, (b) post-v1 (see `spec-proposals/AD-2.md`).
2. **Read-side memory at the 256 MiB cap (A-01, MEDIUM, BL-E008-011).** `verify`/`restore` parse the plain outer
   Manifest before authentication at about 4.3x its size (about 1.1 GiB at the cap). Options: (a) accept and document the
   figure; (b) streaming/incremental parse (already on the backlog); (c) lower the cap (breaks Archives the writer allows).
   Recommendation: (a) now, (b) before any larger Site is supported.
3. **`on_warning` coverage (A-03, MEDIUM, BL-E008-012).** `backup`/`verify` discard the sink; the passphrase-floor and
   command-line-password notices are log-only. Options: (a) document that those two stay log-only; (b) emit both through
   `emit_warning` (CLI then also prints them to stderr). Recommendation: (b), matching "every other operator notice".
4. **`--list-default-excludes` needs `--wp-dir` (A-02 remainder, BL-E008-013; orchestrator decision D1 (a)).** Options: (a) keep
   (now at least no flag is ignored); (b) make `--wp-dir` optional with a post-parse check (changes the generated CLI docs).
   Recommendation: (b); the flag is new, so no v0.2.21 contract binds it.
5. **Restored Site code runs unconfined in the relocation `wp` child (RT-01, MEDIUM, BL-E008-014).** Documented in
   `docs/restore.md` as a trust assumption. Options: (a) accept, keep the documentation and the warning for unencrypted
   Archives; (b) require an authenticated (encrypted) Archive for relocation unless a flag overrides; (c) confine the child
   (bubblewrap or similar, optional). Recommendation: (a) for v1, (c) as an opt-in after.

Orchestrator decisions questioned: the Manifest cap at write time and `--list-default-excludes` ignoring flags (above);
`RootPreflight` routing by requirement name and its placement in `cli/preflight.py` (sound, revisit if a second special
requirement appears); the 11-entry AC narrowed only by footnote (text never edited).

Earlier reviews: R-01..R-03 verified closed; R-07 is not closed (BL-E008-005 family, Low).

## Issues

Resolved: 1 (BL-E008-002 fixed). Fixed without a backlog item: A-02 part, AD-1, AD-3, AD-7 (4). Deferred: 21 new
items BL-E008-011..031 (4 Medium owner decisions, 17 Low), plus BL-E008-003, -005..-009, -010 open. Critical/High: none.
Blocked stories: none.

## Estimate vs actual (all five metrics; see the epic node)

Story estimate was planned before the owner decisions of 2026-11-06/07 widened scope (nine ADR amendments, a new
public `Secret` type, the library changes); `man_hours` was assessed from the diff before reading any estimate.

Actuals: sprint man_hours 84, epic man_hours 86 (formed from the diff before any estimate was read; the sprint estimate was
8.25-9.38 h, the epic 9.7-11.02 h, so the delivered scope is about 8x the plan). Elapsed 1.5 h, HITL 0 (headless), tokens
about 130.9M (cache read 127.9M), model claude-sonnet-5 for closure; the story ran on claude-opus-5 (model mismatch reported by
set-actual). Calibration: man_hours and hitl_hours closure samples were skipped (no estimated overhead; zero residual), orchestration
not separately measurable from this session (recorded as 0).

## Verification at the final tree

- pytest, all of `tests/` without a database: 4835 passed, 137 skipped (every skip is "needs the CI database"; the 4951-test
  MySQL run was on 14029e8). The integration tests that cover `binary_preflight` and the wp probe
  (`test_preflight_refusal.py`, `test_wp_cli_missing.py`) are partly DB-gated, so the owner's `make ci` must re-run them.
- 3.14 isolated run with all extras (`tests/unit`, `tests/contracts`, `tests/adapters`): 4767 passed, 21 skipped.
- ruff check and format, `mypy --strict src`, lint-imports (4 kept), `check_purity`, `check_mode_defaults`, `gen_cli_docs --check`,
  `gen_tool_docs --check`, `uv lock --check`, spec-align `check-links` (25 ADRs OK), `check-dispositions` (8 findings, every Major disposed):
  all green. `tests/contracts/test_closeout_gate.py` is part of the contracts run.
- Not committed (per instruction). `README.md` was regenerated by `gen_cli_docs` for the new help text.
