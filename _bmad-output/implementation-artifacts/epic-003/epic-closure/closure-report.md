# E003 epic closure report

Goal: `wpbackup2 restore` made real. Final status: closed, archived.

## Findings and dispositions

- Arch drift (`arch-drift-review.md`): Blocker 0, Major 1, Minor 5. AD-1 (MAJOR, `performs_effects` was a Dry Run flag under another name) is `resolved-in-code`: the field is gone from `RestorePorts`; the executor accepts a not-performed snapshot from its result alone; the composition root (`cli/restore.py`, where AD-4 permits the Dry Run switch) refuses a not-performed snapshot in a real Run. Regression: `test_only_a_real_run_refuses_a_snapshot_that_was_not_performed` (fails without the guard). AD-2..AD-6 are Low backlog BL-E003-108..112.
- Red team (`redteam-report.md`): CRITICAL 0, HIGH 0, MEDIUM 2, LOW 3, OBSERVATION 4.
  - EP-RT-002 (MEDIUM): fixed. `is_safe_member_path` and `member_relative` now admit only `config/wp-config.php` and `dump/database.sql` under those two roles. Regression: `test_config_and_dump_roles_hold_only_their_one_member` (4 cases fail without the fix).
  - EP-RT-001 (MEDIUM): the red team's own fix direction taken: `docs/restore.md` now says the Site must be quiesced and what the re-survey does and does not catch. Detection of writes to an already non-empty Site is Low backlog BL-E003-113. This is a judgment call: no ADR was written, since no code can fix it without a maintenance-mode design.
  - EP-RT-003/004/005 (LOW): BL-E003-114..116. Observations O1-O4: noted, no action.
- Close-out gate: every E003-owned item (BL-E002-114, BL-E002-116, BL-E003-035/036/037) is closed; `test_closeout_gate.py` passes.

## Fix touches integration tests

`tests/integration/live_restore.py` (RestorePorts construction) and the real-Run snapshot path in `cli/restore.py` changed. Re-run `make ci` under act.

## Metrics

Epic man_hours counterfactual 320, formed from the diff before the estimate was read (6.0k source lines and 11.9k test lines of destructive, security-critical code, a 400-line operator document, five ADR changes, and six red-team rounds). Estimate was 144-164. Closure spend added to the sprint sums.

## Triage

Open Low backlog items for E003: 73 after this closure. None promoted. Blocked stories: none. Spec sync: no items.
