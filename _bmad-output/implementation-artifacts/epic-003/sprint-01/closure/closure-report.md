# Sprint E003-S01 closure
- Stories done: 2 (E003-S01-001, E003-S01-002). Man-hours re-assessment: 70 (formed before the estimate was read).
- Phases run: adversarial + clean-release, red team, UX (CLI wording), arch drift. Retrospective inline (summary in sprint node).
- Fixed in place: v0 duplicate-member sizing (tar.py), symlinked Site Root vs CLEAN keep (restore_runner.py `_same_dir`), Archive config (config-role) laid down through the command port (SD-01-1 resolved-in-code for that path). Regression tests added, each proven failing without its fix.
- Touches what integration tests exercise: yes (restore_runner.py, tar.py); the DB-backed integration tests were skipped here (DB_HOST unset), the orchestrator's MySQL `make ci` run should be repeated.
- Deferred: BL-E003-035..040 (ADV-1 High; RT-001/002/003 Medium; ADV-5..11; UX; Low), owned by E003-S02-003 and E003-S02-006, whose ACs now name the keys.
- Orchestration capture (step 4) not recorded: the orchestrator's own spend is not observable from this agent.
- Gates: ruff, format, mypy --strict, lint-imports, pytest (1822 passed, 32 skipped), check_purity 0, check_mode_defaults 0, gen_cli_docs --check 0, test_closeout_gate 7 passed.
