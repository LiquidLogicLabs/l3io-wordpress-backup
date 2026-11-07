# Epic E008 retrospective (combined sprint and epic closure, 2026-11-07)

Epic goal: bind every earlier epic's providers into the composition root, so every flag reaches its
effect and the library and CLI surface agree. One sprint, one story (E008-S01-001, commit 14029e8,
78 files, +5677/-605 lines).

## What the evidence shows

- **Delivered.** `NOT_YET_IMPLEMENTED` holds only the permanently removed `restore --only-appointed-in-asg`
  (arch review). All four import-linter contracts hold; `check_purity`, `check_mode_defaults` and both doc
  generators pass. `make ci` at 14029e8 was green (4951 passed against MySQL, plus 3.11-3.14).
- **Review yield.** Story review: 1 high, 5 medium, 5 low (fix iteration 1 fixed H-1, M-2, M-3). Closure
  lenses on the same commit found 0 critical/high, 4 medium (A-01, A-02, A-03, RT-01), 3 arch majors
  (AD-1, AD-2, AD-3), and about 20 lows. Nothing in the closure lenses was in the story review; the lenses were not
  redundant.
- **Closure fixes (this pass).** BL-E008-002 (probe cwd), A-02 (`--list-default-excludes` refuses other
  flags), AD-1 (ADR-0018 amendment for `Secret`), AD-3 (docs/api.md prose plus a derived guard), AD-7
  (ADR-0024 first paragraph). Open as owner decisions: BL-E008-003/AD-2, A-01, A-03, RT-01, and the `--wp-dir`
  requirement of A-02.

## Learnings (max 5)

1. **A wiring story is where earlier epics' unreached code shows up.** `BinaryPreflight` had no production
   consumer until this story; the guard that makes `root_preflight` the only construction
   (`test_preflight_construction.py`) is the pattern to keep. Plant a violation that attacks the scope
   (rule 4), not only the rule.
2. **A decision made in conversation but not written to an ADR in the same change is drift.** `Secret` (owner,
   2026-11-07) and the 256 MiB Manifest cap lived only in the story and the commit message; ADR-0018 still
   called the type a backlog item. Record owner decisions in the ADR as part of the change that builds them.
3. **Orchestrator decisions marked "the owner may overrule" produced three of the four Mediums** (cap at write
   time, `--list-default-excludes` ignoring flags, the Safety Snapshot floor). Where a decision narrows an
   acceptance criterion, get the owner's word before the build, not at closure.
4. **Documentation guards must reach prose.** The docs contract checked signatures and headings, so two
   passages describing the removed `context=` survived. `stale_keywords` now derives the live parameter set from
   the public callables.
5. **`run_bounded` replaced `subprocess.run` and lost its guarantees (kill on any exception, bounded reads).**
   When a stdlib call cannot be used, state the guarantees it gave and test each before the first use.

## Process

Single-story epics get little from the sprint/epic split; the combined closure ran each lens once. The long
pole was the full test run (about 2.5 minutes for unit and contract tests; integration tests that need MySQL do not run
without `DB_HOST`).
