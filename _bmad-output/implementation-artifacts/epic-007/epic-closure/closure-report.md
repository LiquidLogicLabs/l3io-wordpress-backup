# E007 closure report (combined sprint 01 and epic closure, 2026-11-07)

Epic goal: move a restored Site to a new URL (relocation flags, config rewrite, WP-CLI search-replace over serialized data, config outside the Site Root, WP-CLI preflight). Final status: done, all four stories (91f8222, 2b3de85, c4059b6, f1cf076) plus the closure fixes listed below (uncommitted; the orchestrator commits).

One pass, one report per lens, in this folder: `adversarial-review.md` (adversarial and clean-release), `redteam-report.md`, `arch-drift-review.md`, `retrospective.md`. Pointers sit in `../sprint-01/closure/` (`epic-closure-pointer.md`).

## Findings and dispositions

| ID | Sev | Disposition |
|---|---|---|
| RT-1 | HIGH | Fixed: `prctl(PR_SET_DUMPABLE, 0)` around every `wp` child (`adapters/wpcli.py::run_child`); docs reworded to say what the minimal environment does not cover. |
| AR-1 | HIGH | Not reachable today: the `wp cli version` probe is not on the CLI restore path until E008-S01-001 binds `BinaryPreflight`. Scheduled there with an AC (BL-E007-007). |
| AR-2 | MEDIUM | `wp-cli.yml` ancestor refusal and state-dir prepare happen at SEARCH_REPLACE. Scheduled to E008-S01-001 (BL-E007-008) because the PREFLIGHT hook it needs is that story's binding. |
| RT-2 = AR-3 | MEDIUM | Timeout fixed (four hours, `TimeoutExpired` becomes `WpCliFailedError`). Output bound scheduled to E008-S01-001 (BL-E007-009). |
| RT-3 | MEDIUM | Fixed: a config laid down by role always excludes the tree's own `wp-config.php` (`restore_runner._extract`). |
| RT-4 = AR-5 | MEDIUM | Fixed: "other" bits taken off a laid-down config, WARNING `config_mode_restricted` replaces `config_world_readable`. |
| RT-5 = AR-4 | MEDIUM | Fixed: stored `siteurl`/`home` pass the URL string rules, new `StoredUrlInvalidError` (exit 3), value not echoed. (Terminal-escape part of AR-4 is covered by refusing control characters; a result line can no longer print them.) |
| ED-1 | MAJOR | Docs fix: `docs/restore.md` carries a "Known gap until E008-S01-001" note. The code fix is E008-S01-001's, now with an AC. Disposition recorded as `resolved-in-code` in spec-align (a docs change; the code fix is E008-S01-001's). |
| AR-6, AR-7, AR-8, RT-6, RT-7, RT-8, ED-2..ED-5, ED-7 | LOW / MINOR | Backlog BL-E007-010 to -020. |
| OBS-1..5 (redteam) | OBSERVATION | Reported below; OBS-3 (doc overstatement) fixed with RT-1. |

Counts: CRITICAL 0. HIGH 2 (1 fixed, 1 scheduled). MEDIUM 5 distinct (4 fixed, 1 scheduled; RT-2's output bound also scheduled). Issues resolved: 6 findings fixed in code or docs. Issues deferred: 4 scheduled to E008-S01-001 plus 11 Low backlog items.

## Fixes touched code an integration test exercises

- `adapters/wpcli.py::run_child` and `_Session.stored`: exercised by `tests/integration/test_search_replace.py` (real `run_child`, real WP-CLI 2.12). Not run here: this host has no `wp`, `php` or MySQL. Real stored values there are plain `http://` URLs, which pass the new rules; the dumpable flag is reset by `execve`, so the child is unaffected.
- `adapters/restore_runner.py::_extract` and the new `_restrict_laid_down`: exercised by `tests/integration/test_config_outside_root.py` and `test_config_parent_resolve.py` (need the CI database). They set no mode assertion on the laid-down file; their v0 archives have no config-role member and take the unchanged in-tree path.
- The orchestrator's CI run on the committed tree is the check for these.

## Regression tests, and proof each fails without its fix

Added in `tests/adapters/test_wpcli.py` (stored-URL refusal x12, trailing slash kept, timeout mapping, real-runner timeout, `/proc` unreadable with a control) and `tests/unit/adapters/test_restore_runner.py` (mode narrowing x4, config above the root excludes the tree's copy). With the source changes to `wpcli.py` and `restore_runner.py` stashed, 18 of them fail (all the new ones except the trailing-slash case, which passes either way and guards against over-refusal); the shadowing test failed on its own in the same way. Restored, all pass.

## Gates (on the working tree with the fixes)

ruff check and format, `mypy --strict src`, lint-imports (3 kept), `check_purity`, `check_mode_defaults`, `gen_cli_docs --check`, `gen_tool_docs --check`, `uv lock --check`, `tests/contracts/test_closeout_gate.py`: pass. `pytest tests/unit tests/contracts tests/adapters`: 4210 passed, 20 skipped (CI database). Touched tests on Python 3.14 isolated: 776 passed, 1 skipped. `pyproject.toml` and `uv.lock` untouched. Not run here: the integration suite and the 3.12/3.13 legs.

## Owner decisions (batched)

1. **O4(a) (root warns only): red team recommends refusing as root.** Restored Site PHP then runs as root, and RT-1 shows it can read the process's environment; a warning is easy to miss in cron. Options: (a) keep warn-only, (b) refuse unless an explicit `--allow-root-wp-cli` flag is given. Recommendation: (b). Owner decision is final; recorded as disagreement only.
2. **`--new-db-password` on argv (AD-22, accepted).** It is readable in `/proc/<pid>/cmdline`, the only credential on the process list this epic owns. Options: (a) keep, (b) accept it from an environment variable or file descriptor for restore too. Recommendation: (b), as a new story in a later epic. No fix possible here without a flag change.
3. **Orchestrator decision to question:** AR-1/AR-2/BL-E007-009 were scheduled to E008-S01-001 rather than fixed here (not reachable on the live path, or the fix needs the preflight hook that story binds). If you want them closed in E007, say so.

Other observations: OBS-4 (a pre-planted `wp-cli.yml` in `$HOME` refuses every relocating restore for that user, exit 3) is safe behaviour, no change; the E007-S01-004 story's AC on `domain/preflight.py` placement of `WP_CLI_REQUIREMENT` differs from the code (ED-3, Low).

Orchestration actuals: the orchestrator's own overhead is not visible to this session, so the orchestration block was written as zeros. Replace it if a measured figure is wanted.

## Estimate vs actual

Written to the sprint and epic nodes; see `pm-status show --epic E007` (archived) for all five metrics. `man_hours` counterfactual formed from the diff before the estimate was read: 90 h (about 1,900 source lines of security-sensitive adapter and core code, about 5,000 lines of tests, docs). The estimate was read afterwards: 43.62 to 49.57 h.
