# Adversarial review: E008-S01-001 (commit 14029e8)

Lens: adversarial, with the clean-release checklist as also_consider.
Content: `git show 14029e8` (src diff `~/.cache/e008cl/src.diff`, 3632 lines).
Method: read every src hunk; reproduced the claims marked "reproduced" on the working tree with `uv run --offline`. No source edited, nothing committed.
Not re-reported: R-01..R-11 of `closure/review-E008-S01-001.md`.

Counts: CRITICAL 0, HIGH 0, MEDIUM 3, LOW 8. This is below the lens's ten-finding expectation for MEDIUM and above. I re-checked the surfaces listed in "Checked, no finding" rather than pad the list.

## Status of earlier findings

- R-01 (process group not killed on interrupt): closed. `run_bounded` now kills the group on any `BaseException` from the wait (`adapters/preflight.py` ~L345-355).
- R-02 (reader join unbounded): closed. `_join` has a 2 s total grace; readers are daemon threads.
- R-03 (doctor failed on wp-cli.yml): closed. `BinaryPreflight(optional=True)` returns a `ToolStatus.error` instead of raising.
- R-04, R-05, R-06: still open as recorded (BL-E008-002/003/004). The code is unchanged at the cited lines. See A-01, which extends the R-05 memory point.
- R-07: still open. `--new-db-password ""` still falls through to the environment (reproduced for the env case). The empty-file case also returns `None` (reproduced).

## Findings

### A-01 MEDIUM [adversarial] Pre-authentication memory amplification at the new 256 MiB Manifest cap

- File: `src/l3io/wp/backup/domain/archive_format.py:123` (cap), `src/l3io/wp/backup/adapters/tar.py:836-845` and `:1415` (whole-member reads), `domain/archive_format.py:388` (`_loads`).
- Evidence: the cap rose from 16 MiB to 256 MiB. `_walk` keeps the first member `manifest.json` in memory when `info.size <= MANIFEST_MAX_BYTES`, decodes it to `str`, then `json.loads`. I measured `json.loads` on a 12.8 MB member list: peak 54.8 MB, a 4.3x factor before `Manifest`/`ManifestMember` objects are built. The existing test uses `MEMORY_FACTOR = 25` (review R-05), which is about 6.4 GiB at the cap.
- The outer Manifest is read before any passphrase is used. A hostile or corrupted Archive of a few hundred MiB makes `verify` and `restore` allocate gigabytes before authentication. The decision (E004 D2 (c)) names the cap and the refusal at write time, not this cost on the read side, and the streaming parse is on the backlog only.
- Fix: until the streaming parse exists, read the Manifest through an incremental parser with a member-count bound, or lower the read-side cap and keep 256 MiB only for the write-side refusal. At minimum record the memory figure in `docs/` and in the owner decision.

### A-02 MEDIUM [adversarial] `backup --list-default-excludes` still requires `--wp-dir` and ignores every other flag (orchestrator decision D1 (a))

- File: `src/l3io/wp/backup/cli/backup.py` `run_backup` (L704-712 of the diff hunk), `cli/parser.py:222`.
- Evidence (reproduced): `wpbackup2 backup --list-default-excludes` exits with `error: the following arguments are required: --wp-dir`. With `--wp-dir` given, `run_backup` prints the defaults and returns before `_exclusion` and `_cipher`, so `--exclude 'bad['`, `--encrypt-with-passphrase` and `--archive` are accepted and ignored. The story states this ("the other backup flags are ignored").
- The brief says no flag may be accepted and ignored. A listing command that needs a Site path it never reads, and silently drops an invalid pattern, is the exact "accepted and ignored" class. The orchestrator decision was labelled owner-overridable.
- Fix: make the combination a `UsageError` (`--list-default-excludes` takes no other backup flag), or make `--wp-dir` not required when the flag is present (a custom `required` check after parse). The first keeps AD-22's frozen surface.

### A-03 MEDIUM [adversarial] `api.backup` and `api.verify` accept `on_warning` and discard it

- File: `src/l3io/wp/backup/api/operations.py` (`del on_warning` in `backup` and `verify`).
- Evidence: both entry points take `on_warning` and delete it on the first line. `docs/api.md` says they "give no notice today". The owner decision gave all three entry points the parameter, so the signature is not in question. The effect is that a caller passing a sink to `backup` gets no notice from it, with no error.
- Concrete gap: `passphrase_below_encryption_floor` (`cli/passphrase.py::note_short_restore_passphrase`) and `password_on_command_line` (`cli/credentials.py::new_db_password`) are operator notices the CLI surfaces through the log only. They never become a `RunWarning`, although the decision text says "every other operator notice the CLI writes". `restore` calls `note_short_restore_passphrase` and the sink never sees it.
- Fix: route those two notices through `emit_warning` in `restore` (they are real notices); for `backup`/`verify` either document the no-op in the parameter docstring or pass the sink to whatever notice they have. Add a contract test that every `on_warning` parameter is read.

### A-04 LOW [adversarial] `Secret` made `ExplicitCredentialSpec` and `RelocationFlags` unhashable and not copyable

- File: `src/l3io/wp/backup/domain/secret.py:23` (`__hash__ = None`, `__reduce__` raises), `domain/credentials.py` (`frozen=True` dataclass holding a `Secret`).
- Evidence (reproduced): `hash(ExplicitCredentialSpec('u','abcdef'))` raises `TypeError: unhashable type: 'Secret'`. `copy.deepcopy` and `dataclasses.asdict` of a `RelocationFlags` holding a password raise `TypeError` (`__reduce__`). Before this commit both worked. Both are exported from `api`. The decision says not hashable "as its plain string"; it does not say the containing public dataclasses lose `hash`.
- Fix: give `ExplicitCredentialSpec` and `RelocationFlags` `__hash__` over the non-secret fields plus a digest-free marker, or document in `docs/api.md` that they are unhashable and uncopyable. Define `__deepcopy__` returning the same `Secret` (immutable) if copying should work.

### A-05 LOW [adversarial] `--new-db-password-file` keeps a UTF-8 BOM, and the "readable only by you" help is not enforced

- File: `src/l3io/wp/backup/cli/credentials.py` `_read_password_file` (opens with `encoding="utf-8"`), `cli/parser.py` help text.
- Evidence (reproduced): a file starting with `EF BB BF` yields a 7-character password for `abcdef` and the value does not equal `"abcdef"`. The relocated account is then provisioned with a password the operator did not write. The help text says "make it readable only by you" but nothing checks mode `0600`, ownership, or that the path is a regular file (R-08 covers only the size bound).
- Fix: open with `utf-8-sig`; refuse (or WARN, naming the path) a file that is group- or world-readable, as `ssh` does for keys.

### A-06 LOW [adversarial] `Secret.__eq__` raises on a lone surrogate

- File: `src/l3io/wp/backup/domain/secret.py:48`.
- Evidence (reproduced): `Secret('a\udcff') == Secret('a\udcff')` raises `UnicodeEncodeError` (`.encode()` without `errors=`). Values from `os.environ` or `input()` under a non-UTF-8 locale contain surrogates. The only call site is the prompted-passphrase confirmation in `require_passphrase`, so the user gets `UnexpectedError`/a traceback instead of a match or `PassphraseMismatchError`.
- Fix: `.encode("utf-8", "surrogatepass")` (or `surrogateescape`) on both sides.

### A-07 LOW [adversarial] `run_bounded` signals a process group after its leader was reaped

- File: `src/l3io/wp/backup/adapters/preflight.py:353-357`.
- Evidence: after `process.wait()` returns, the leader is reaped, then `_kill_group(process.pid)` sends `SIGKILL` to that number as a pgid. If no member of the group is left, the number can be reused by an unrelated process group in the window between reaping and `killpg`. The suppressed `ProcessLookupError` covers a free number, not a reused one. The window is small. The consequence is a kill signal sent to an unrelated process (the same user's, or root's if the tool runs as root).
- Fix: call `killpg` before `wait()` reaps (use `waitid(..., WNOWAIT)` or `os.waitid` with `WEXITED | WNOWAIT`), then reap.

### A-08 LOW [clean-release] Dead construct left in `run_bounded`

- File: `src/l3io/wp/backup/adapters/preflight.py:323`.
- Evidence: `with contextlib.nullcontext():` wraps the body (a leftover from the removed `with Popen`). It does nothing and indents 40 lines of logic.
- Fix: delete the `with` and dedent the block.

### A-09 LOW [adversarial] `doctor` blockers carry only an exit-code name

- File: `src/l3io/wp/backup/cli/doctor.py:85` (`_site_readiness`).
- Evidence (reproduced): a `wp-config.php` with invalid UTF-8 makes `doctor --wp-dir ... --json` report `database_clients.blockers: ["CREDENTIALS"]` and exit 7. The cause (unreadable config) is not reported. A missing config or an unreadable one would give another bare name. The brief for doctor is to tell the operator what to fix.
- Fix: carry the error class and the package's own redacted message as a second field (for example `blocker_details`), without values.

### A-10 LOW [adversarial] `restore` flag combinations accepted and resolved silently

- File: `src/l3io/wp/backup/cli/restore.py` (the `require_encrypted` branch, then the `allow_unencrypted` branch; `RestoreRequest.allow_unencrypted`).
- Evidence (code read, not run): `--allow-unencrypted` with no passphrase has no effect and no notice. `--allow-unencrypted --require-encrypted` is refused by the `require_encrypted` branch that runs first, with a message that does not mention the contradictory flag. The brief asks that no flag is accepted and ignored.
- Fix: `UsageError` when `--allow-unencrypted` is given without a passphrase source or together with `--require-encrypted`. Same in `api.restore`.

### A-11 LOW [adversarial] A raising `on_warning` sink aborts a restore after the Safety Snapshot is taken

- File: `src/l3io/wp/backup/cli/restore.py` (snapshot callable, `emit_warning(... safety_snapshot_*)`).
- Evidence (code read): `emit_warning` runs after the snapshot is `performed`, inside the snapshot step. The documented rule is that the sink's exception propagates. A sink that raises (a logger with a closed handle, a UI callback) aborts the Run after the snapshot exists and before the destructive phases, leaving a snapshot and a journal entry with no restore. The notice for an unauthenticated Archive is before any effect, so it is harmless; the snapshot notices are not.
- Status: PLAUSIBLE (not run).
- Fix: call the sink for the snapshot notices after the snapshot step returns, or catch `Exception` from the sink there, log `warning_sink_failed`, and continue.

## Checked, no finding

- Clean-release scan of the added lines: no TODO/FIXME/XXX/HACK, no `breakpoint`/`pdb`, no commented-out code, no credential-like literal in src or tests (test passwords are fixtures of the form the hygiene guards expect). The `noqa` markers carry reasons. The only `print` additions are `emit_warning`'s stderr line and the existing skipped-links line.
- `.reveal()` appears only in `credential_providers.py`, `crypto.py`, `redaction.py` and `secret.py`, as the guard states.
- Short-credential refusal: applied in the explicit step, the registering wrapper (all chain sources, wp-config included) and `new_db_password`. The floor is `MIN_SECRET_LENGTH = 4`, per the ADR amendment in force.
- Encryption: 12-character floor and double prompt are applied only on the encrypting path (`require_passphrase`); the prompt mismatch check runs after the length check; the env and prompt sources both reach the floor.
- Flag wiring: `--db-credentials-aws-*` (backup, restore), `--admin-credentials-aws-*` (restore, `run_restore` L1773-1777), `--exclude`, `--no-default-excludes`, `--encrypt-with-passphrase`, `--new-db-password-file`, `--allow-unencrypted` each reach an effect; `--db-credentials-aws-region` alone is refused (reproduced). The remaining `NOT_YET_IMPLEMENTED` entry is the permanently removed flag.
- Doctor: invalid `L3IO_WP_BACKUP_PASSPHRASE_BACKEND` is reported as `invalid`, never echoed; a malformed or directory-shaped `wp-config.php` yields `NOT_READY` (exit 7) with the diagnosis, not a traceback (reproduced).
- Manifest cap: the measurement uses the production serialiser with the payload fields at their longest form (sound); the write-side refusal timing and the Dry Run gap are R-05.
