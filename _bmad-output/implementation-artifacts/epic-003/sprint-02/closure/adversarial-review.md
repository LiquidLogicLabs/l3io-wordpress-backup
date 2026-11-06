# Adversarial + clean-release review: E003 Sprint 02 (4d85bd3..ae7a10d, src + docs)

Lens: adversarial (bmad-review), plus clean-release checklist (dead code, commented-out code, debug
artifacts, TODO markers, secrets). Scope: /tmp/s02-src.diff (19 src/docs files). Tests were not
reviewed. Line numbers are in the working tree at ae7a10d. Owner decisions (ADR-0016 amendment
2026-11-06: unauthenticated Archives restore with a warning) are treated as final and not re-raised.
No credential values were read or printed.

Clean-release sweep result: no TODO/FIXME/XXX/HACK, no commented-out code, no breakpoint/pdb, no
hard-coded secret. The `print(...)` calls (cli/restore.py) are deliberate stderr warnings. The `noqa`
S105 hits are labels/key names. Findings below are the exceptions.

## Findings

### ADV-S02-01 | adversarial | HIGH | cli/restore.py:1241 (+ core/restore.py:883,889)
The journal header always gets `snapshot_path=str(snapshot_path)`, even when the Safety Snapshot is
skipped (`--no-safety-snapshot`) or not needed (target empty). `_snapshot_undo` and `_prior_values`
branch on `snapshot_path is None` to say "opted out", so that branch is unreachable from the CLI.
Scenario: `--mode all --cleanfirst --force --no-safety-snapshot`, the Run dies in EXTRACT. The refusal on
the next restore says "restore files from Safety Snapshot <path>", a file that was never written, and
a DB-cutover undo says DB_PASSWORD "from Safety Snapshot <path>". Wrong recovery guidance after a
destructive failure, in exactly the case where the operator has no snapshot.
Fix: set `snapshot_path` only when `RestorePhase.SNAPSHOT in plan.phases` (and keep the executor's
ValueError check for that case); add a test for skip and for not-needed.

### ADV-S02-02 | adversarial | HIGH | cli/restore.py:801-822 (+ core/restore.py:1090-1119)
`_bind_archive` copies the whole Archive into the workspace before PREFLIGHT, so before any free-space
check, and `space_need`'s workspace term (`dump + payload + config_copy`) does not include the copy.
Scenario: a multi-GB Archive and a small tmpfs/`/tmp` workspace: the copy fills the filesystem and fails
mid-copy with `ArchiveWriteError` (or starves later decrypt/dump extraction that the check "passed"). The
BL-E002-116 guarantee ("refuse before CLEAN what the destinations cannot hold") is not true for the new
largest consumer.
Fix: check `free_bytes(workspace)` against `archive.size` (plus the existing workspace need) before
copying, and add the Archive size to the workspace claim in `_check_space`/`check_claims`.

### ADV-S02-03 | adversarial | MEDIUM | adapters/restore_runner.py:629-635
`_destroys` decides "would this restore destroy?" by comparing `type(adapter).__module__` with
`noop.__name__`. A wrapper, subclass or test double defined in another module counts as destroying, and
a real adapter re-exported through `noop` would count as harmless. This is the gate that lets a
not-performed snapshot through (ADV-5).
Fix: give the adapter set an explicit `performs_effects` property (or compare against a `NoopMarker`
protocol) and test the Dry Run and real sets against it.

### ADV-S02-04 | adversarial | MEDIUM | adapters/staging.py:358-415 (`_cutover`)
Only `ConfigWriteFailedError` on the first key drops staging and journals `cutover_readback_failed`. Any
other exception from `set_constants` (OSError, a library error) for the first key leaves
`cutover_started:<KEY>` as the last step with the staging database retained and no `written` info; the
undo text then tells the operator "the cutover may have landed; do not drop <load>". Also
`STEP_CUTOVER_STARTED + key` is journaled before `_cutover_value` runs, so a `CredentialsError` for
DB_PASSWORD (relocated credential fetch fails) leaves the same ambiguous state.
Fix: resolve the value before journaling `cutover_started`; handle every exception from the write the
same way as the first-key ConfigWriteFailedError (read back, then classify).

### ADV-S02-05 | adversarial | MEDIUM | adapters/staging.py:590-640 (`complete_cutover`)
On `--resolve-interrupted-run` the cutover set is completed without re-checking that the load
database still exists and still passes the WordPress check, and `header.cutover_targets[key]` is a bare
index (KeyError, unhandled, for a hand-edited or older-format journal). Between the interruption and
the resolve, the staging DB can have been dropped by the operator following the "drop staging" undo
text of an earlier step.
Fix: survey the load database before writing and refuse (REFUSED) if it is not WordPress; use
`.get` with a CorruptJournalError.

### ADV-S02-06 | adversarial | MEDIUM | cli/restore.py:1317-1325 (+ 1339-1344)
`_carry_snapshot` runs only for `BackupError`. A non-BackupError (OSError, KeyboardInterrupt, a bug)
after a performed snapshot propagates without the snapshot path, so the one message that tells the
operator where the copy of the destroyed site is, is lost. `run_restore` (1434) has the same limit.
Fix: catch `BaseException` in `drive_restore`, attach/print the path, re-raise; or emit the path as soon
as `snapshot_result` returns (it already logs `safety_snapshot_written`).

### ADV-S02-07 | adversarial | MEDIUM | cli/restore.py:1403-1405
`resolve_passphrase(prompt=terminal_prompt())` runs on every restore, before the Archive is probed. On a
TTY the operator is asked for a passphrase when restoring an unencrypted Archive (and an accidental
entry silently makes the Safety Snapshot encrypted under a passphrase they may not remember).
Fix: resolve lazily after the probe: prompt only if `view.encrypted`; for an unencrypted Archive use
env/argument only, and say in the snapshot message which choice was made.

### ADV-S02-08 | adversarial | MEDIUM | cli/restore.py:1392-1397, 1226-1246 (Safety Snapshot at rest)
Without a passphrase the Safety Snapshot (full site files incl. `wp-config.php` with DB credentials,
and a DB dump) is written unencrypted to the state directory. It is warned about on stderr, but there
is no retention or cleanup, no documented mode check on the published file (this diff sets 0700 on
directories; the file's mode is the store's), and `--new-db-password` is accepted on argv (visible in
`ps`) although AD-26 prefers the environment, with only a registry-redaction mitigation.
Fix: document the file mode and verify it in `snapshot_result`; add a `--new-db-password` env/stdin
alternative before Epic 7 un-gates the flag; document snapshot retention in docs/restore.md.

### ADV-S02-09 | adversarial | MEDIUM | adapters/journal.py:102-110, 368-373
The journal is keyed by `sha256(canonical Site Root)` under the per-user state dir. Two OS users (or
root and a service account) restoring the same Site each get their own directory, so one never sees the
other's open journal; the Site Lock is the only cross-user protection and it is not in the journal path.
Also `begin_run` uses `exists()` then write (non-atomic; safe only under the lock) and
`_ensure_dir` applies `0700` only to the leaf (`parents=True` creates intermediates with the umask).
Fix: document "one user per Site" or lock the journal directory; open `current.json` with `O_EXCL`.

### ADV-S02-10 | adversarial | MEDIUM | adapters/staging.py:418-470 (`_loser`) + core/restore.py:765-775
After a successful same-server restore the Site permanently runs on `<live[:51]>_l3s_<run8>` and the
original database is dropped (Mode permitting). Anything outside this tool that names the original
database (cron dumps, monitoring, another app's grants, the previous backup jobs of this tool) breaks
silently, and docs/restore.md does not state that DB_NAME changes by design. Design consequence of AD-18,
so only the documentation is in question.
Fix: state it in docs/restore.md and print it in the human report ("the site now runs on X (was Y)" is
there; add "update anything else that names Y").

### ADV-S02-11 | adversarial | MEDIUM | adapters/database.py:170-200 (`_tls_settings`)
`--admin-tls` can only raise the policy; TLS is mandatory to every non-loopback host. A MariaDB on a
trusted LAN or Docker network without TLS cannot be restored to at all, and there is no explicit,
logged opt-out. (Owner decision territory; reported as an operability gap, not a defect.) Related:
`UsageError(str(exc))` at 197 and 396 forwards the dependency's message text, contrary to the "never
database's text" rule in `_tls_error`.
Fix: record the gap in ADR-0015/restore.md, or add `--admin-tls insecure-i-know`; map the
InvalidArgumentsError to a fixed message.

### ADV-S02-12 | adversarial | LOW | adapters/site_files.py:77-90 (`tree_bytes`) + adapters/restore_runner.py:545-555
The docstring of `_snapshot_claim` calls the value "an uncompressed upper bound", while `tree_bytes`
says "a lower bound" (unreadable entries and directories are skipped, `os.walk` has no `onerror`) and
sparse files count apparent size. The claim is therefore neither bound. A snapshot of an unreadable
subtree also silently omits it.
Fix: correct the comment; count unreadable entries and refuse (or warn) if any.

### ADV-S02-13 | adversarial | LOW | core/restore.py:~960 (`undo_for`, REWRITE_CONFIG) 
`f"copy {header.saved_config} back to {config}"` prints `None` when `saved_config` is None (the header
only fills it for OVERWRITE, but the branch is by step prefix `config_`). Also `_load_undo` for
`step is None` tells the operator to drop a staging database that may not have been created yet.
Fix: guard None; say "if it exists".

### ADV-S02-14 | adversarial | LOW | adapters/restore_runner.py:~790-800 (`_archive_config`)
`path.unlink(missing_ok=True)` followed by `O_EXCL|O_NOFOLLOW` opens a check/act gap in the workspace;
low risk (0700 dir, owner-only), but the unlink is redundant given the O_EXCL contract and masks a
pre-existing file instead of failing loudly.
Fix: drop the unlink, let O_EXCL fail.

### ADV-S02-15 | adversarial | LOW | cli/restore.py:1187
The STRUCTURAL_ONLY warning is a bare `print` to stderr emitted in `drive_restore`, which the docstring
promises "never prints" (the only declared exception is the snapshot-encryption warning, which is a
second print at ~1297). Not a defect given the owner decision; the contract text is inaccurate and the
library root of Epic 6 will inherit stderr writes.
Fix: return the warnings in `RestoreOutcome` (or accept a `warn` callback) and print in `run_restore`.

### CR-S02-01 | clean-release | LOW | cli/restore.py:663, 1439, 1392-1397
Unreachable or placeholder code for Epic 7: `RESTORE_EXTENSION = None` with `_NoExtension`,
`RestoreExtensionFactory`, `RelocationFlags`, the `new_db_password` registration (twice: 1394 and in
`drive_restore`) and the "relocation not available" branch are all unreachable from the CLI because
parser.py still gates those flags with `NOT_YET_IMPLEMENTED`. It is intentional scaffolding but is shipped
untested-by-use in this release; the double registration of `new_db_password` is redundant.
Also `require_database_restored(outcome, outcome, ...)` passes the same object as two parameters
(`_Phased`, `_Reported` protocols exist only to allow that).
Fix: register once; take a single `outcome` parameter; mark the seam in docs or defer to Epic 7.

### CR-S02-02 | clean-release | LOW | docs/restore.md:50
Docs say a database is non-empty with "at least one table or view"; code since BL-E003-053
(core/restore.py `_survey_empty`, `_contents`) also counts routines and events. Stale doc.
Fix: "at least one table, view, routine or event".

### CR-S02-03 | clean-release | LOW | adapters/restore_runner.py:~690 (`snapshot_result`)
Parameter named `bytes` shadows the builtin (suppressed with `noqa: A002`); the post-publish
`verify_archive` failure message includes `str(path)` and the first failure reason only.
Cosmetic; rename to `size_bytes` at the one call site (cli/restore.py ~1226).

## Counts
CRITICAL 0, HIGH 2, MEDIUM 9, LOW 7 (total 18: 15 adversarial ADV-S02-xx, 3 clean-release CR-S02-xx).
