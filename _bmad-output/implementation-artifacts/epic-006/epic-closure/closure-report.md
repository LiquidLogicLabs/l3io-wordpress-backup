# E006 closure report (combined sprint 01 and epic closure, 2026-11-07)

**Goal:** Embed it in Python automation (the library surface a third party builds on). **Status:** done; archived. No blocked stories.

Reports (written once, over the whole epic): `retrospective.md`, `adversarial-review.md`, `redteam-report.md`, `arch-drift-review.md`, `drift-dispositions.yaml` (this folder). Pointer: `../sprint-01/closure/retrospective.md`.

## Estimate vs actual (epic)
| Metric | Estimate | Actual |
|---|---|---|
| man_hours (counterfactual, formed before reading the estimate) | 48.47 - 55.08 | 88 (stories 59 + closure 29) |
| elapsed_hours | 16.15 - 18.36 | 2.97 (+ 3.2 orchestration) |
| hitl_hours | 2.75 - 3.12 | 0 |
| tokens_k | 1135 - 1290 | 130,130 (cache-read 125,776); orchestration 238,861 separately |
| cost | derived | 62.72 (+ 108.09 orchestration); stories 88.96 were priced at opus-5 |

The token estimate is two orders of magnitude under the actual: it counts neither cache reads nor the orchestrator's long context. A calibration issue, not an E006 defect. Skips on write: man_hours zero residual at epic level (single sprint, one re-assessment), hitl_hours zero residual (no human time anywhere), model mismatch (estimate priced at opus-5, closure ran at sonnet-5).

## Velocity
1 sprint, 4 of 4 stories, carry-over 0.

## Findings and disposition
Severity rule applied: a reachable HIGH/MEDIUM is fixed here; otherwise backlog or owner decision. Counts: adversarial 1 HIGH / 4 MEDIUM / 8 LOW; red team 1 HIGH / 2 MEDIUM / 3 LOW / 4 OBS; arch 0 BLOCKER / 3 MAJOR / 3 MINOR. The three lenses overlap heavily (about 9 distinct defects of MEDIUM or above).

**Fixed here (8 findings, one source change each; regression tests verified to fail without the fix):**
- AR-1 / RT-4 (HIGH): `BackupError.__reduce__` rebuilds by state, so every package error survives `pickle`/`copy` and a `ProcessPoolExecutor`. Test: `tests/contracts/test_exception_pickle.py` (derived set, real constructors, real pool).
- AR-2 / RT-1 / AD-3 (HIGH / MAJOR): `public_boundary` no longer passes `exc_info`; the record carries `traceback` frames with non-package messages withheld (`cli.exit.render_failure`). Test: `test_the_unexpected_record_carries_frames_and_never_the_original_text`.
- RT-2 (MEDIUM): span status for a non-package exception names the class only. Test: `tests/adapters/test_tracing.py` (updated assertion).
- AR-3 / RT-5 (MEDIUM / LOW): `PR_SET_DUMPABLE` toggle is reference-counted under a lock. Test: `tests/unit/adapters/test_wpcli_dumpable.py`.
- RT-3 (MEDIUM, dependency): the false "no exception event reaches a span" claim corrected in `adapters/tracing.py`, `docs/api.md`, ADR-0011 amendment; request to the dependency's owner is BL-E006-010.
- AD-2 / AR-5 (MAJOR / MEDIUM): the false "never prints" claim corrected (`api/operations.py`, `docs/api.md`); behaviour unchanged, see OD-2.
- AD-1 (MAJOR): ADR-0025 accepts `api/` on the `cli/` drivers (`adr-justified`).
- AD-6 and part of AD-5: ADR-0011 marked Accepted with the O-2(a) amendment and the beta exporter recorded; ADR-0018 amended for the boundary record.
- AR-4 documented in `docs/api.md` (secrets in frames), and AR-13 / dumpable / stderr behaviours documented; not fixed in code.

**Touches integration-tested code:** `adapters/wpcli.py` (dumpable guard), `domain/errors.py` (`__reduce__`), `api/errors.py` and `adapters/tracing.py`. Their integration tests need `DB_HOST` and were not run here (skipped: no CI database); unit, contract and adapter tests ran.

**Backlog (BL-E006-008 to BL-E006-023, 16 items; Medium: 008, 009, 010; rest Low):** none were put in E008-S01-001 as an AC, because each Medium waits on an owner decision (008, 009) or on another package (010), and E008-S01-001 is a different story's scope; say so if you want them moved.

## Owner decisions (batched)
1. **OD-1, ADR-0025 (AD-1).** `api/` composes through `cli/` drivers. Options: (a) ratify (done as orchestrator decision); (b) extract the drivers, `RunContext`, request/result types and passphrase chain to a neutral module. Recommendation: (a) now, (b) before a second front end (BL-E006-021).
2. **OD-2, stderr from `restore` (AD-2/AR-5, BL-E006-008).** (a) keep and document (done); (b) add a caller-supplied warning sink, default stderr for the CLI, logger-only for the library, and amend AD-26. Recommendation: (b) before v1.0; a library should not print.
3. **OD-3, `backup(target=None)` writes to the host's cwd (RT-6, BL-E006-011).** (a) keep; (b) make `target` required in the API like `scope`. Recommendation: (b).
4. **OD-4, secrets as plain `str` (AR-4/RT-7, BL-E006-009 and -017).** (a) document only (done); (b) an opaque `Secret` type for `passphrase`, `ExplicitCredentialSpec.password` and `RelocationFlags.new_db_password`, with `__reduce__` guards. Recommendation: (b), a public-API change that is cheapest before v1.0.
5. **OD-5, `context=` is public but cannot be obtained (AR-12, BL-E006-012).** (a) remove the parameter until an opener is exported; (b) export the opener. Recommendation: (a). Also AR-7 (BL-E006-014) goes away with it.

**Orchestrator decisions to question:** the man_hours figure of 88 (formed from the diffs before the estimate was read); splitting closure tokens between sprint (the parallel-review window) and epic (the rest) because the dispatch brackets overlapped; accepting ADR-0025 without the owner.

## Outstanding issues
Low: 13 in this epic's closure (BL-E006-011 to -023, less none promoted); Medium: 3 (BL-E006-008 to -010). Triage: no Low was promoted.

## ADRs produced
ADR-0025 (new); amendments to ADR-0011 and ADR-0018; ADR README updated.

## Spec changes
`sync-plan` returned no items; `check-pointers --all` clean (32 stories). Dispositions: AD-1 adr-justified (ADR-0025), AD-2 and AD-3 resolved-in-code. Spec sync used no tokens.

## Gates at closure
ruff, format, mypy --strict, lint-imports (4 kept), check_purity, check_mode_defaults, gen_cli_docs and gen_tool_docs --check, `uv lock --check`, test_closeout_gate: pass. Full suite: 4654 passed (one failure found and fixed: a parameter named `args` below a root). Python 3.14 isolated, `--all-extras`: 4589 passed. Not run: MySQL-backed integration tests (no `DB_HOST`).
