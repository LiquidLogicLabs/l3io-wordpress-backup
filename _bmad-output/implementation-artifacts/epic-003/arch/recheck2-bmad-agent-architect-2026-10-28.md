# Arch gate second re-check — E003, `bmad-agent-architect` (Winston), 2026-10-28

- **Question per finding:** do `24b31a1` (cycles, AG-17) and `7b78347` (AG-1, AG-3) resolve what my first re-check left open?
- **Scope:** AG-1, AG-3, AG-17 and the dependency-cycle MAJOR. I did not open the other reviewer's recheck files.
- **Line numbers** refer to the files as they are at `7b78347`.

| ID | Was | Verdict | Evidence |
|---|---|---|---|
| AG-1 | BLOCKER, residual MAJOR | **resolved** | **Intent steps.** Before each `set()`, the step `"cutover_started:<KEY>"` is journaled (E003-S02-005 L78). Before `revoke_grants`/`drop_database`, the step `"loser_drop_started"` is journaled (L81). Both have their own undo rows (L112, L116). The `"cutover_started"` row says "do not drop `<load_database>`" and points to `--resolve-interrupted-run`. That command re-reads which keys landed (L152).<br>**The window I reported is closed.** A kill after `os.replace` of `DB_NAME` but before `"cutover:DB_NAME"` is written now leaves `"cutover_started:DB_NAME"` as the last step. That state no longer says to drop the live database.<br>**The harness reaches the window.** The wrapped `ConfigCommand` and `restore_database_command` now signal after each effect lands, so the kill point *k* can fall between the effect and its journal entry (L210). The assertion names the `"cutover_started"` row (L215).<br>Two MINOR defects in the wording that came with this fix are listed under New findings. Neither reopens the destructive window. |
| AG-3 | BLOCKER, residual MAJOR | **resolved** | E003-S01-001 now declares every sprint-02 symbol I listed, in `core/restore.py` (L61–L67): `snapshot_scope`, `snapshot_location` with `UnsafeSnapshotLocationError`, `SnapshotResult`, `DatabaseRestoreReport`, the journal step-name constants, and `is_orderly_failure`. Their tests moved with them (L164).<br>**`LOAD_DUMP` is an injected slot.** The new parameter is `load_database: Callable[[RestoreInputs, RunJournalCommand], DatabaseRestoreReport]` (L68). The arm has no other logic (L105), and the executor never imports `adapters/staging.py`.<br>**Sprint-01 tests need no sprint-02 code.** They pass a fake slot and a fake `take_snapshot` (L165). The integration test's slot fails the test if it is called (L167).<br>E003-S02-003, -004 and -005 now cite these declarations rather than define them, and E003-S02-006 binds the real slot with `functools.partial(load_staged, ports=ports)` (L66).<br>E003-S01-002's remaining `E003-S02-*` mentions are citations only: binding by the root, and CLI assertions that moved out of it. The administrator-bound `survey_target` it needs is E002-S02-009's.<br>**Answer: yes.** Sprint 01 can be built and close green with no sprint-02 module. Its state nodes list no `E003-S02-*` dependency. |
| AG-17, and the cycles MAJOR | MAJOR | **resolved** | I walked every `depends_on` in all 32 `state/planned/*/*/E*.yaml` nodes with a DFS: no cycles and no dangling targets.<br>**E003 edges:** S01-002→S01-001; S02-003→S01-001; S02-004→{S01-001, S02-005}; S02-005→S01-001; S02-006→{S01-001, S01-002, S02-003, S02-004, S02-005}.<br>**Fixes confirmed:** `E003-S02-005.yaml` no longer lists S02-004, and `E003-S02-004.yaml` no longer lists S02-006.<br>**Extraction file:** the `edges:` list in `planning-artifacts/dependency-extraction.yaml` has the same E003 subgraph and no cycle that involves E003. Three inverted candidates (S01-001→S02-005, S01-002→S02-004, S02-005→S02-003) remain only under `uncertain:`. They are not adopted into the graph. |

## New findings

- MINOR: The undo rule contradicts itself (E003-S02-005). L101 says "after the first `"cutover_started:*"` step the text never names `header.load_database` as something to drop". But the `"cutover_readback_failed"` row (L110) comes after a `"cutover_started:<KEY>"` step, and it says "drop staging database `<load_database>`". The row is the correct behaviour, because nothing landed and the original config is intact. Restate L101 as "after a `"cutover_started:*"` step with no later `"cutover_readback_failed"`", or "after the first `"cutover:*"` step", so `undo_for` and `test_restore_journal.py` are not written against the wrong sentence.
- MINOR: ADR-0020's amended consequence (L70–L73) claims too much. It says the harness signals after "every config write and database command" and that "every effect whose interruption changes the undo is therefore preceded by an intent step". Neither is true of `EXTRACT`'s config lay-down:
  - E003-S01-001 L102 journals `"config"` only *after* `SiteFilesCommand.extract`.
  - `SiteFilesCommand` is not one of the wrapped ports (E003-S02-005 L210).
  - So a kill between laying the config down and writing its step yields the `CLEAN`/`EXTRACT` "restore files from Safety Snapshot" row, which omits "remove `<wp_config_path>`".
  - The result is not destructive, but the operator is not told about a stray config, and the ADR's prose stops the next reader from looking.
  - Either add an intent step (`"config_started"`) or narrow the ADR to the cutover and the loser drop.
- MINOR: The order of E003-S02-005 and E003-S02-004 is now unclear for the report.
  - `7b78347` makes E003-S02-005's `load_staged` return `DatabaseRestoreReport`. Its `restored` field is computed by "E003-S02-004's pure derivation" (E003-S01-001 L65), and E003-S02-004 "populates it" (E003-S02-004 L69).
  - Yet E003-S02-004 now follows E003-S02-005 (AG-17).
  - So the S02-005 dev loop must build the report with no stated rule for `restored`, `previous_database` or `ensure`.
  - Either state that S02-005 builds the report with an interim derivation that S02-004 replaces, or move the pure derivation into E003-S01-001 next to the type.
- Note (no new severity): `load_staged(inputs, journal, *, ports)` is now the complete statement of the slot's signature. The Dump's `MemberCipher` and the `SecretRegistry` therefore have no carrier unless `RestorePorts` gains them. That is already filed as BL-E003-025, which now gates this signature.

Sections read:
- `epic-003/arch/recheck-bmad-agent-architect-2026-10-28.md` (all)
- `git show 7b78347` (stat and word-diff of all 7 files)
- `git show 24b31a1 --stat` and its `state/planned` changes (as current files)
- E003-S01-001 L56–L110 and L160–L180
- E003-S01-002 L52–L60 and L100–L110
- E003-S02-004 L45–L75
- E003-S02-005 L60–L125, L145–L160 and L200–L235
- E003-S02-006 L57–L66 (grep)
- `docs/adr/0020` L60–L80
- `docs/adr/0021` (the diff hunk)
- `state/planned/epic-003/**` (all nodes, both `sprint.yaml`)
- DFS over all `state/planned/*/*/E*.yaml`
- `planning-artifacts/dependency-extraction.yaml` (header, E003 `edges`, `uncertain:`)
- `state/issues.yaml` (BL-E003-021..026 titles)
- grep for the administrator source in E002-S02-009 and E002-S02-012

DONE — resolved 3/3, new: 0 BLOCKER, 0 MAJOR, 3 MINOR
