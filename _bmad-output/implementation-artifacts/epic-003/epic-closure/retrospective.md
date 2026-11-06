# Epic E003 retrospective (restore)

Evidence: sprint-01 and sprint-02 closure reports, the sprint-02 retrospective, `git log a42ec0d..fea5a21` (19 commits, 137 files, +22734/-396), and the story files under sprint-01/stories and sprint-02/stories. Source code was not read.

## Scope delivered

Six stories done, none blocked: S01-001 (restore plan and executor, size preflight), S01-002 (refuse destructive restore over a non-empty target), S02-003 (Safety Snapshot), S02-004 (honest database restore report), S02-005 (survive kill mid-restore: staging, cutover, Run Journal), S02-006 (wpbackup2 restore end to end, TLS to the administrator). Extras: BL-E003-053 and the RT-002 owner decision (ADR-0016, ADR-0022).

## Velocity

- Sprint 01: 2 stories, 70 man-hours re-assessment (formed before the estimate was read). Gates: pytest 1822 passed / 32 skipped.
- Sprint 02: 4 stories after a re-estimate from calibration (fd602ea). Man-hours were not recorded in the closure report.
- Commit shape: 19 commits since the epic baseline. 6 are feature commits (one per story plus BL-E003-053), 2 are sprint-closure or chore commits, and 6 are closure and post-closure fixes. So about a third of the epic's commits came after the stories were called done.
- Post-closure: 6 commits since e3bea72 (e767a66, c2c4896, bc38d95, dd34cb0, 5fbc2b0, fea5a21) closed every High and Medium backlog item. Sprint 02 had deferred 39 items (1 High, 14 Medium, 24 Low); only Low items are known to remain. The final test count is 2443 passed / 85 skipped on Python 3.11 and 3.14, and `make ci` gave 2516 passed against mysql80.
- Orchestrator spend was not observable, so there is no cost-per-story figure. This is a known gap in the sprint-01 closure report.

## What went well

- Every closure fix carried a regression test shown to fail without it.
- Owner decisions were taken as ADRs (0016, 0021, 0022, 0023, 0024) and then settled backlog items, e.g. ADR-0021 closed BL-E003-055 without code.
- Deferrals were assigned to named stories (BL-E003-035..040 to S02-003 and S02-006) and their acceptance criteria were amended to name the keys.
- A Critical was caught before release: the mysql client ran client commands from an untrusted Dump (RT-S02-001).

## Recurring pain points

1. Security findings landed at sprint closure, not at the story that introduced the exposure. The Critical (RT-S02-001) and the admin-password channel High (RT-S02-002) both involve feeding untrusted Archive content or secrets to an external process. The sprint-02 retrospective names this itself.
2. Guards that checked prose rather than behaviour (RT-S02-002: a help-text check stood in for a check on what the code does). The same pattern recurred in drift SD-02-1, where adapter-module introspection stood in for an explicit dry-run flag.
3. The password and confirmation channel needed three rounds: RT-S02-001/002 in closure, then RT-S02-003 (confirmation could be pasted back; password-exposing auth plugins) and RT-S02-004 (an existing database account reset without consent) afterwards.
4. Integration coverage was uneven. DB-backed tests were skipped in sprint 01 (DB_HOST unset) and had to be repeated by the orchestrator; sprint 01 and 02 both fixed bugs in integration-exercised code at closure.
5. Backlog accumulation: 39 deferrals after sprint 02 turned into a dedicated fix pass. Many were operator wording (UX) and file or directory permission findings that could have been done with the stories.
6. Orchestration capture and man-hour actuals were not recorded, which makes velocity hard to state.

## Process improvements

- Run a threat pass (red team lens) at the first story that sends Archive content or a credential to an external process, not only at sprint close.
- Write behaviour guards: a test must exercise the behaviour, not assert help or docs text. Keep doc-contract tests (as in tests/contracts/test_restore_docs.py) for docs only.
- Always run the DB-backed integration suite in closure; make a skipped DB_HOST a closure blocker for stories that touch integration-exercised code.
- Settle owner decisions as ADRs early, in the elaboration of the story that raises them, so they do not sit as Medium backlog items.
- Budget a hardening slice in each sprint plan so deferrals are bounded, rather than relying on an end-of-epic pass.
- Record man-hour actuals and orchestration cost in the closure report.

## Top 3 learnings

1. Untrusted input and credentials need a threat pass at the story that first routes them to an external process. Closure is too late and cost three more fix rounds.
2. A guard on description is not a guard on behaviour. Tests and checks have to act on the thing that can go wrong (the code path, the file mode, the process arguments).
3. Owner decisions recorded as ADRs convert open Medium backlog into closed items quickly. Deferrals with a named owner story and amended ACs were all closed; unowned ones needed a separate pass.

## Acceptance

E003 is accepted as complete for High and Medium findings. The remaining known work is Low-severity backlog and the items re-homed to E007-S01-001 and E007-S01-002 (BL-E003-056, BL-E003-057). Follow-up: confirm the Low count in the issues file; it was not read here.

## Assumptions

- Man-hours for sprint 02 are not in the named evidence, so none are stated.
- The commit categories above are counted from commit subjects in `git log a42ec0d..fea5a21`.

## retrospective_summary

Epic E003 delivered six stories that make wpbackup2 restore real, covering the restore plan, the destructive-restore refusal, the Safety Snapshot, an honest database report, kill-safe staging with a Run Journal, and the end-to-end command. About a third of the epic's commits were closure and post-closure fixes, driven by security findings found late, including one Critical, and a post-closure pass then closed every High and Medium backlog item. The main lesson is to threat-review untrusted input and credential handling at the story that introduces it, and to test behaviour rather than prose.

## retrospective_learnings

- Run a threat pass at the first story that passes Archive content or a credential to an external process, not at sprint closure.
- A guard must act on behaviour (code path, file mode, process arguments), not on help text or prose.
- Record owner decisions as ADRs during story elaboration so they do not become open Medium backlog items.
- Give every deferral a named owner story with amended acceptance criteria; unowned deferrals needed a separate fix pass.
- Make the DB-backed integration suite and man-hour or cost capture mandatory closure steps.
