# UX Review (Reviewer Gate) — sprint E003-S01

Scope: user-visible refusals, messages and exit codes of E003-S01-001/002 (`git diff a42ec0d..10193bf -- src docs`), `docs/restore.md`, and the stories' UX/acceptance criteria. Lens: CLI product — message clarity, actionable next step, no credential leakage, vocabulary consistency. Method: single reviewer reading the code and docs; nothing was executed and no source was modified. Note: `cli/restore.py` does not exist yet (E003-S02-006), so no message reaches a terminal in this sprint; findings concern the strings and contracts that root will render.

Credential leakage: none found. `NonEmptyTargetError` names paths and `host:port/name` only (`core/restore.py:220-251`); `RestorePorts.__repr__` hides the credential callable (`ports/restore.py:41`). No finding.

## Findings

### UX-1 HIGH — The FR-7 refusal names flags in domain spelling, not the flags the operator types
`src/l3io/wp/backup/core/restore.py:248-251`, `:208-217` (`render_flags`). Message ends `mode=files force would authorize it`. AC for E003-S01-002 requires the refusal to "name the flag that would authorize it" / "the exact flag". The operator types `--mode files --force`; `mode=files force` is not copy-pasteable, and `force` without dashes is ambiguous. Story dev notes defer the CLI spelling to `cli/restore.py` (E003-S02-006), but the shipped `NonEmptyTargetError.__str__` is what the exit path prints today and the AC is marked done. Impact: the headline safety message of the sprint does not meet its own AC until S02-006 lands.
Next step: S02-006 must override the rendering (the error already carries `.authorizing`); add that as an explicit AC with a test asserting the stderr text contains `--force`.

### UX-2 HIGH — Refusal names the internal Mode, not the `--mode` value the operator gave
`core/restore.py:248`: `refusing to restore 'files-clean' over a non-empty target`. `--mode` accepts files/database/all (parser.py), and "clean" comes from `--cleanfirst`/`--drop-database`. Quoting a Mode value the operator never typed (and `docs/restore.md:7-19` is the only place the six names are defined) breaks vocabulary consistency between flags, message and docs.
Next step: render the Mode as the flag combination the operator supplied, or cite "Restore Mode `files-clean` (--mode files --cleanfirst)".

### UX-3 MEDIUM — `docs/restore.md` does not document the FR-7 non-empty-target refusal at all
`docs/restore.md:29-42` ("Before anything is changed") lists preflight and verify only; `--force` and `--cleanfirst` appear once, in passing (`:4`, `:73`). The sprint's second story (refuse destructive restore, exit `REFUSED`, per-part specificity, both parts named in one refusal, no interactive prompt) has no user documentation. `--whatIf` section (`:91-97`) also omits that the guard refuses in a Dry Run.
Next step: add a "Non-empty targets" section: what counts as non-empty, the flag per part (`--force`, `--drop-database`), that waiving files never waives the database, exit 3, and an example message.

### UX-4 MEDIUM — Space and expansion refusals give raw byte counts and no next step
`core/restore.py:182-204`. `needs 1073741824 bytes and has 52428800 bytes free (49807360 usable after the 5% margin)`: unreadable at GB scale, "restore target (Site Root filesystem)" (`:134`) and "Run workspace filesystem" (`:135`) name no path, so the operator cannot tell which mount to free; the workspace message gives no hint how to relocate it. `ExpansionRatioError` says "over the 200x expansion limit" with no advice (suspect archive; no override exists, which the message should say).
Next step: include the path, humanised sizes (keep raw bytes in structured fields), and one sentence of remedy ("free N MiB or choose another `--wp-dir`/workspace location"; "this limit has no override").

### UX-5 MEDIUM — `ArchiveHasNoDumpError` / `ArchiveHasNoConfigError` state the problem but not the way out
`domain/errors.py:517-524`, `core/restore.py:172-179`. "archive 'x' holds no database dump (scope 'files')" — does not say that `--mode files` would work, or that a v0 archive ("a v0 archive") means an old format the operator may not recognise. The config message ("holds no wp-config.php to overwrite the target's with") omits that dropping `--overwrite-wp-config` is the fix. The docs (`restore.md:26-27`) also omit the remedy.
Next step: append "use --mode files" / "omit --overwrite-wp-config".

### UX-6 MEDIUM — Exit-code documentation inconsistent
`docs/restore.md:26`, `:65` say "exit `REFUSED`" with no number while `:37-38` give `VERIFICATION_FAILED (8)`. REFUSED is 3 (`domain/exit_codes.py:49`). Scripters need numbers. There is also no table of which refusals are 3 vs 8 vs 1 (e.g. unreadable Site Root exits FAILED, not REFUSED — BL-E003-034 — which contradicts the story's Outcome text and should be in the docs until fixed).
Next step: put numbers on every code mention or link one table.

### UX-7 MEDIUM — Interpreter/extraction refusals surface internal wording
`adapters/tar.py` (`_UNPATCHED_TARFILE`, ~:1051) is actionable (names versions, says upgrade), good; but the same error class renders as `extraction refused: this Python lacks...` with no member, and the per-member refusals read `extraction refused at member 'x': its header size 5 exceeds the indexed 4` / `the running total passes the indexed N bytes` ("indexed" is internal Manifest vocabulary). Nothing tells the operator the archive may be corrupt or tampered with, or to re-verify it.
Next step: reword to "the member's size differs from what the Archive's Manifest records; the Archive may be corrupt or tampered with — run `wpbackup2 verify`" (confirm the verify subcommand name).

### UX-8 LOW — "changed between planning and verification" / "target changed after planning"
`adapters/restore_runner.py:115-123`, `:125-133`. "nothing was changed" is reassuring, but "planning" is internal; add a retry hint ("the archive was modified while the restore started; re-run").

### UX-9 LOW — Documentation jargon undefined for operators
`docs/restore.md` uses Run workspace, Run Journal, Site Root, Dump, Mode capitalised as terms without a glossary link or the flag/path that sets them (`--snapshot-dir`, journal dir location unstated at `:80`). Link a glossary or define on first use.

### UX-10 LOW — Symlink skip is WARNING-only, exit `OK`
`docs/restore.md:84-89`, `adapters/tar.py:975`. Documented and deliberate; but a restore that silently omits links exits 0. Consider a one-line stderr summary count at end of Run (S02-006) so it is not only in logs. Suggestion, not a defect.

### UX-11 LOW — Consistency of quoting
Messages quote values with `!r` (single quotes, Python repr) in some errors (`'files-clean'`, `'/srv/www'`) and without in others (filesystem names). Pick one convention in the S02-006 renderer. Cosmetic.

## Verdict
No credential leakage; refusals are fail-safe and mostly say "nothing was changed" (a strength) and the FR-7 refusal names every unauthorized part in one message (meets the AC). Two HIGH items (UX-1, UX-2) concern spelling of flags and mode in the safety refusal and should be closed before or as part of E003-S02-006; the docs gap (UX-3) should be closed in this sprint's closure.

Counts: HIGH 2, MEDIUM 5, LOW 4.
