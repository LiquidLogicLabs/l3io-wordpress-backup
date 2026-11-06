# UX review: sprint 02, epic E003 (CLI surface of `wpbackup2 restore`)

Scope: `cli/restore.py`, `cli/parser.py`, `domain/errors.py` message changes, `docs/restore.md`, from `/tmp/s02-src.diff`; stories E003-S02-003, -004, -006. Lens: the bmad-ux Reviewer Gate, run as one inline pass (rubric walker only). No `_bmad-output/planning-artifacts/ux*` exists, so there is no DESIGN.md or EXPERIENCE.md to check against. The stories' UX criteria and the sprint-01 UX findings (UX-1..UX-7) were the baseline. `wpbackup2 restore --help` was run, plus five probe invocations against an empty temp directory. No source was edited.

Assumption: `--log-format` defaults to JSON when stderr is not a terminal, as `--help` says, so the probes below show the non-terminal experience.

Counts: HIGH 0, MEDIUM 5, LOW 8.

## Verified as good

- UX-1 and UX-2 are fixed. The non-empty-target refusal names the CLI mode and the flags that would authorize it. `--drop-database` without `--cleanfirst` is refused with the exact corrected command lines.
- `ArchiveHasNoDumpError` now points to `--mode files`.
- Dry Run output is worded "would ..." and never "restored".
- The "NOT encrypted" snapshot warning goes to stderr.
- The snapshot path is repeated when a later phase fails.
- Credential flag help warns that `--admin-password` is visible in the process list and names the environment variable.

## MEDIUM

**UX-S02-01. The unauthenticated-Archive warning exists only as a log record for the common case.**
Evidence: `cli/restore.py` ~1179-1187. For every unencrypted, checksummed Archive (the default backup), the only signal is `_log.warning("archive_unauthenticated", integrity=..., archive=...)`. That record has no human sentence and no advice. `--log-level ERROR` suppresses it. Under JSON logging it is one line among many. Only STRUCTURAL_ONLY gets a plain stderr sentence.
Why it matters: RT-002 was ratified as "restores, with a warning", so the warning is the control. A warning the operator can silence by log level, or never see in a log stream, weakens that control.
Recommend: print one stderr sentence for every unencrypted Archive, independent of log level. For example: "WARNING: this archive is not encrypted, so its checksums do not prove who wrote it; restore only archives from a source you trust, or encrypt your backups." Keep the structured record as well. Add a test that the sentence appears at `--log-level ERROR`.

**UX-S02-02. `--whatIf` after `restore` is rejected with a generic error; `restore --help` and the docs do not mention it.**
Evidence: `wpbackup2 restore --whatIf ...` gives `unrecognized arguments: --whatIf` (exit 2) and prints the top-level usage. It is accepted only before the subcommand (`wpbackup2 --whatIf restore`). `docs/restore.md` line 3 says the command line, including `--whatIf`, is "documented by `wpbackup2 restore --help`", which does not list it. The Dry Run section writes "`--whatIf`" as if it were a restore flag.
Why it matters: the dry-run is the main safety feature of a destructive command, and the natural spelling fails.
Recommend: accept the flag on the subcommand too, or at least add to `restore --help` and the docs "global option, give it before `restore`: `wpbackup2 --whatIf restore ...`". Include the full invocation in the Dry Run section.

**UX-S02-03. Flags that do nothing yet appear in `restore --help` as working.**
Evidence: `--wp-config`, `--new-site-url`, `--new-site-home-url`, `--new-db-host`, `--new-db-name`, `--new-db-user` and `--new-db-password` are listed with live-sounding help. Using one exits 2 with "not available in this build: delivered by Epic 7 (relocation)". `--only-appointed-in-asg` is described as "Removed with chesney; accepted only to explain that".
Why it matters: the operator learns a flag is inert only by being refused, and "chesney" and "Epic 7" are internal names.
Recommend: put "(not yet available)" in each flag's help, or hide them behind a "relocation (planned)" group. Reword the error to describe the capability ("restoring to a new URL or database is not yet supported"), not the epic.

**UX-S02-04. Output mixes plain text and JSON under `--log-format json`, and a successful files-only restore prints nothing.**
Evidence: `emit_database_report` switches to JSON, but `search-replace: N replaced`, `safety snapshot: <path>`, `wp-config.php kept (...)` and the Dry Run actions are always plain text on stdout. In a JSON consumer, stdout is a mix. In a real run, there is no final "restore complete" line naming the mode, archive and site. A files-only restore with no snapshot and no existing wp-config prints nothing, and the only evidence is exit 0 and the log stream.
Why it matters: scripts cannot parse stdout reliably. Interactive users get no confirmation.
Recommend: emit one summary object or line on stdout (mode, archive, site root, snapshot path, database, search-replace) in the configured format, and end with a one-line completion message in text mode.

**UX-S02-05. `docs/restore.md` never maps flags to the Restore Mode names it uses everywhere.**
Evidence: the Modes table uses `files`, `files-clean`, `database`, `database-clean`, `all`, `all-clean`. The flags are `--mode db`, `--cleanfirst`, `--drop-database` and `--force`. Nowhere does the doc say which flags select `database-clean` or `all-clean`. A refusal message says "(Restore Mode files)", the same word as the flag value, which is confusing for `db`/`database`.
Recommend: add a "Flags" column to the Modes table (for example `database-clean` = `--mode db --drop-database`; `all-clean` = `--mode all --cleanfirst --drop-database`). Add one worked example for each of a normal run, a Dry Run and a refusal, showing real output.

## LOW

**UX-S02-06. Usage errors on `restore` print the top-level usage, not the restore usage.** Evidence: the output of `restore --wp-dir X` (no `--archive`) and of `--drop-database` without `--cleanfirst` starts with the `wpbackup2 [-h] ... {backup,restore,...}` line. Recommend using the subparser's usage (or "see `wpbackup2 restore --help`").

**UX-S02-07. `--archive` is required in practice but shown as optional in the usage line (`[--archive ARCHIVE]`).** It is enforced after parsing. Recommend `required=True` on the argument, or a help note.

**UX-S02-08. The docs' exit-code table omits 7 `NOT_READY`, which `--help` lists.** Preflight refuses for missing tools or an unsafe Python, but the docs say these are `REFUSED`. Check which code is actually returned and make the docs and the table agree.

**UX-S02-09. STRUCTURAL_ONLY is printed as a plain-text line on stderr between JSON log lines.** Under JSON logging this breaks line-by-line parsing. Emit it through the logger as well, or only as the stderr sentence required by UX-S02-01 with a documented format.

**UX-S02-10. On failure, `safety snapshot: <path>` goes to stdout while the error goes to stderr.** An operator who looks only at stderr, or redirects it, misses the recovery path. Include the path in the error message itself, or print it to stderr.

**UX-S02-11. Database report wording.** "loaded 183927 bytes in 412 ms" gives raw bytes (use a human size). The report lists internal provisioning steps ("WordPress user created", "access granted") before the outcome. Put "database: restored" or "NOT restored" first, or make it the last line only.

**UX-S02-12. Open from sprint 01: UX-4 (core refusal messages in domain spelling, BL-E003-058) and UX-7 (tar error wording, BL-E003-059) remain open as Low.** No new sprint 02 regression was found. They need a story with write access to `core/restore.py` messages and `adapters/tar.py`.

**UX-S02-13. `--resolve-interrupted-run` help does not say that it stops after resolving.** The docs do ("resolving and restoring are never one command"). Add this to the help, so the operator does not expect the restore to continue.
