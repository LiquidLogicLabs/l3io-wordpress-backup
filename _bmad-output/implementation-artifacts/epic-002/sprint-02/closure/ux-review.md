# UX review: E002-S02-007, E002-S02-008, E002-S02-011

Scope: CLI surface (flags, help, errors, exit codes, dry-run output) in `cda0b46..dadafe1`.
Method: direct review (the `bmad-ux` skill produces DESIGN/EXPERIENCE documents and has no reviewer gate usable here). None of the three stories has a separate UX acceptance-criteria section; the "Outcome" and edge-case sections were used as the criteria. Behaviour was exercised by running `.venv/bin/wpbackup2` against a scratch site with an unreachable database. The lock-refusal text was read from `domain/errors.py::SiteLockedError`, not run.

Counts: HIGH 2, MEDIUM 5, LOW 4.

## What works (for context)

- All 24 restore flag combinations are table-driven. Rejections exit 2 and name the combination that would work, for example `--cleanfirst clears the Site Root, which --mode db does not restore; to remove the previous database use --mode db --drop-database`.
- `--db-credentials-aws-region` without the secret id, and a lone `--admin-username`, are rejected at parse time with an actionable reason.
- `--only-appointed-in-asg` gets its own explanation, not "unrecognized arguments".
- `--help` carries a generated exit-code table. `--version` works.
- A Dry Run refused by a query still prints its plan to stdout and exits with the real code (5 in the test).

## Findings

### UX-1 HIGH: `backup --whatIf` is rejected, and the error gives no hint
`--whatIf` is registered only on the top-level parser, so it must come before the subcommand. `wpbackup2 backup --whatIf --wp-dir site` gives `unrecognized arguments: --whatIf`, exit 2, and the usage line shown is the top-level one. Operators read `backup ... --whatIf` as a backup option. The same rejection will hit anyone who appends the flag to a working backup command line (the natural way to rehearse). The v0.2.21 position is frozen, so keep it, but also accept it after the subcommand, or at minimum make the error say "put --whatIf before the subcommand". `backup --help` does not list it either, so the flag is undiscoverable from the screen where it matters. Story 007's outcome writes the command as `wpbackup2 --whatIf backup`, so the story does not cover this gap.

### UX-2 HIGH: A Dry Run is not labelled, and its output is a plan that reads like a result
The plan prints bare, numbered, with no header such as "DRY RUN: nothing was changed". Steps 5 to 7 say `write Archive`, `verify` and `publish to file:...`, in the present tense, with no marker that none of them happened. The story's own non-goal says "the report says it is a plan, not a rehearsal of the write", but no such statement is emitted. Piped or redirected into a log, the output is indistinguishable from a completed backup's. Also, on success the real backup prints the archive path and the Dry Run prints the plan, so scripts cannot tell the two apart from stdout alone. Add a header or trailer, and say that write-time failures (disk full, an unreadable file) are not detected.

### UX-3 MEDIUM: "not available in this build: delivered by E003-S02-006" shows internal tracking to operators
`restore` (whole subcommand), `doctor`, `--encrypt-with-passphrase`, `--exclude`, and the rest print story IDs (`E003-S02-006`, `E008-S01-001`) or "Epic 7". These mean nothing to an operator. The story requires naming "the feature and the story", so this is to the letter, but the message should say what the feature is and when it will be available, with the ID as secondary text. Also, `restore --wp-dir site --force` and a plain `restore` report only that restore is unavailable. That is correct, but `restore --help` advertises roughly 20 flags and gives no sign that the command itself does nothing. Put "not yet available in this release" in the restore and doctor help lines and in the epilogue.

### UX-4 MEDIUM: Dry Run plan leaks implementation names and gives vague lines
- `check dump readiness (DatabaseQuery.check_dump)` names a Python method.
- `excluding nothing, none` is a confusing phrase. The real meaning is "no exclusion patterns configured".
- `verify` and `publish to file:/path` use the internal `file:` scheme. They also appear in a files-only plan (`--mode files`), where nothing is dumped or verified for a database.
- The plan does not show the mode (files/db/all) or whether it is encrypted.
- Lines carry no hint of which steps a Dry Run runs for real (credentials, readiness) and which it skips. Mark them, for example `[checked]` and `[skipped]`.

### UX-5 MEDIUM: Failure output on non-TTY is JSON only, and a Dry Run plan is interleaved with INFO-level JSON logs
With stderr not a terminal, the default log format is json. A Dry Run therefore emits `phase_completed`, `backup_completed` and `run_finished` JSON records for steps that were skipped. In the files-only test those four JSON lines (on stderr) appeared above the plan, and the plan was the only human-readable part. Errors also arrive as a single JSON object, with the human message inside a `message` field that itself starts `wpbackup2: error:` and ends with `[exit 5 UNREACHABLE: ...]`. This is a defensible design for cron, but it is undocumented in the stories reviewed. State the rule in `--help` for `--log-format`, and consider printing the final error as plain text on stderr regardless of log format (stdout and stderr are both used for this, so scripts keep a stable contract).

### UX-6 MEDIUM: Site-lock refusal has no recovery guidance, and the holder's start time is only an ISO string
`SiteLockedError` reads `site '/x' is locked (held by run <uuid>, pid N on host, since <ISO time>; lock file '<hash>.lock')`. It names the holder, as the story requires, but gives no next step ("wait for it to finish; check `ps -p N`; the lock clears itself if the process is gone"). For an unidentified holder it says only "the holder could not be identified". The lock file path is a sha256 name, which helps nobody. The README documents exit 9 in a table but nowhere explains the lock, where it lives, or that other OS users are not excluded (a stated non-goal in story 008 that an operator must be told, since a cron job running as a different user than the manual run is the common case). Not exercised live in this review.

### UX-7 MEDIUM: README documents the flags but not the behaviours these stories introduced
`README.md` gained the regenerated help text, but a grep for `lock`, `whatIf` and `dry run` finds only help-screen copies and the exit-code rows. There is no section on: how to rehearse a backup; what a Dry Run does and does not check; the Restore Mode table (`--mode` x `--cleanfirst` x `--drop-database` x `--force`) that is the central safety contract of story 011; or the `L3IO_WP_BACKUP_ADMIN_PASSWORD` variable that two help strings point to. The table is the thing an operator needs before running a destructive command.

### UX-8 LOW: Several restore options have no metavar
`--admin-username ADMIN_USERNAME`, `--new-site-url NEW_SITE_URL`, `--admin-credentials-aws-secret-id ADMIN_CREDENTIALS_AWS_SECRET_ID` produce a 100-column usage line. Other flags use short metavars (`ID`, `REGION`, `PATH`). Use consistent short metavars (`USER`, `URL`, `HOST`, `NAME`, `ID`, `REGION`). The restore usage block in `README.md` is wrapped at 100 columns only because the author hand-formatted it; real `--help` output wraps differently.

### UX-9 LOW: Help text style is inconsistent
Some help strings start lower-case (`use this wp-config.php instead of searching for it`, twice), some end with a full stop (the verify flags), most do not. `--only-appointed-in-asg` says "Removed with chesney", a name operators do not know. The `--db-credentials-aws-*` group on `backup` and `restore` is a bare group; the restore options group has no description line explaining that it holds the non-mode flags.

### UX-10 LOW: `--drop-database` help refers to a mode in a way that needs a second read
`Remove the previous database once the restored one has taken over. --mode all needs --cleanfirst with it` does not say that `--mode db --drop-database` is itself valid, nor that a lone `--drop-database` with the default `--mode all` is refused. The refusal message is excellent; the help should link to the same wording, or `restore --help` should print the table of the six valid modes.

### UX-11 LOW: A missing Site Root reports exit 1 FAILED with a bare relative path
`backup --wp-dir nonexist` exits 1 (`FAILED`, "a reason no other code names") with `invalid site: site root does not exist (nonexist)`. Exit 3 (`REFUSED`) or 2 would describe a bad argument better than the catch-all, and the message should show the absolute path it tried (relevant when the working directory differs under cron). Existing behaviour mapped by story E002-S01-005, noted here because 007 and 008 both rely on resolving this path first.

## Not found
- No flag added by these stories takes a passphrase value.
- Exit codes seen (2 for usage, 5 for unreachable, 1 for a missing Site Root, 0 for a files-only Dry Run) match the documented table.
