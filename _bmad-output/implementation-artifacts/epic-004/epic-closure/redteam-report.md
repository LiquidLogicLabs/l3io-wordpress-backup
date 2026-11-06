# Red-team report — epic E004 (epic-level)

Date: 2026-11-06. Scope: commits 6f3c5c4 (exclusions), 6defdbe (v0 fixtures / legacy reading), 3a1d1eb
(passphrase-only age encryption, inner-Manifest binding), traced through `adapters/tar.py`,
`adapters/crypto.py`, `adapters/restore_runner.py`, `cli/{verify,restore,passphrase}.py`,
`domain/archive_format.py`, `core/exclusions.py`, ADR-0024 (incl. the 2026-11-06 amendment).
Method: five lenses (external attacker, malicious insider, chaos engineer, abusive legitimate user,
design) plus the AI-poisoning cross-cut, done manually (the skill's sanctum/research steps were not run;
no source, tests, pyproject or lockfile were modified). Probes ran with `uv run python -I` / the
shipped `wpbackup2` against archives built in `/tmp/rt` (outside the repo).

**Reachability baseline.** `wpbackup2 backup --help` marks `--encrypt-with-passphrase`, `--exclude`,
`--no-default-excludes` and `--list-default-excludes` "not available in this build". So today the CLI
cannot write an encrypted Archive or apply exclusions; what is reachable is `verify`
(keyless and `--authenticate`) and `restore` over encrypted and plain Archives (made by the library
API or by hand), plus plain backups. Findings say which.

ADR-0024 owner decisions (passphrase-only, the inner-Manifest option A, the rollback limit) were not
re-litigated. The rollback limit is disclosed in `verify --help`, README.md:270, docs/restore.md:213 and
the `verify` pass line (`cli/verify.py` `ROLLBACK_LIMIT`). No code deviation from the amendment found.

## Findings

### RT-1 HIGH — Encrypted-to-plain downgrade by substitution is accepted by restore (warning only)
- Where: `src/l3io/wp/backup/cli/restore.py:1290-1304` (`if not view.encrypted:` prints a stderr
  warning and continues); `adapters/restore_runner.py:591` (decrypt/authenticate only `if inputs.view.encrypted`).
- Attack: whoever can write the Storage Target (the RT-2 threat the amendment addresses) replaces an
  encrypted Archive with a forged **unencrypted** one (own Manifest, own `payload.tar.gz`, matching sha256,
  e.g. `site-root/shell.php` plus a hostile Dump). The operator's restore has the passphrase in
  `L3IO_WP_BACKUP_PASSPHRASE` as always (it is resolved on every restore, to encrypt the snapshot), but
  nothing says "this archive was supposed to be encrypted". Restore proceeds, writes the web shell to the
  Site and loads the Dump. In cron/DR runs the stderr warning is not seen. The amendment's binding protects
  only archives that are encrypted; it does not stop an attacker choosing not to be.
- Evidence: built `plainforge.tar` (forged plain v1 with a `shell.php` member). Keyless `verify` →
  `OK, integrity VERIFIED`, exit 0. `verify --authenticate` → `ArchiveNotEncryptedError` exit 2 (so verify
  refuses it, correctly). `restore` has no equivalent refusal; code path read, not executed end to end.
  (A flag flip `encrypted:false` on a real encrypted payload is rejected `UNREADABLE`, exit 8 — that
  variant is closed.)
- Reachable: yes, `restore` today. ADR-0016's 2026-11-06 amendment waives authenticity for plain Archives;
  this finding is the narrower gap that an operator who *only ever uses encrypted Archives* has no way to
  say so. The documented rollback limit does not cover it, and the docs do not mention it.
- Fix: add `restore --require-encrypted` (and a library/env equivalent) that refuses a plain Archive
  before any effect; consider making it the default whenever a passphrase was supplied explicitly and
  requiring `--allow-unencrypted` otherwise. Mention it next to the rollback limit in docs/restore.md.

### RT-2 MEDIUM — Memory exhaustion from a tar extended-header bomb in a gzip Archive (keyless verify, restore probe)
- Where: `adapters/tar.py:791-813` (`_stream_outer`: `tarfile.open(mode="r|")` over a `GzipFile`),
  `_walk` (747). stdlib `tarfile` reads a GNU longname/pax header body fully into memory with no cap, so
  the repository's "bounded memory, stream everything" claim (AD-21, module docstring) does not hold for
  header bodies. The same applies to the inner payload during `extractall` / `read_member` for a plain
  Archive (attacker-chosen), and to `read_inner_manifest` only after authentication (needs the passphrase).
- Attack: a 0.8 MB `.tar.gz` whose first member is a GNU `L` header declaring 800 MiB of data.
- Evidence: `uv run wpbackup2 verify --archive bomb.tar.gz` ran > 2 minutes and reached **5.8 GB RSS**
  before I killed it (host protection). Scales linearly; a 4 MB file can ask for several tens of GB.
- Reachable: yes — `verify` (no passphrase needed) and `restore` (it copies and probes the Archive first).
  Needs the ability to place a file the operator will verify/restore.
- Fix: before `tarfile` sees the stream, bound the extended-header size (a `TarFile` subclass overriding
  `_proc_gnulong`/`_proc_pax`, or a pre-scan of 512-byte headers that refuses type `L`/`K`/`x`/`g` bodies
  over e.g. 1 MiB), or cap total decompressed bytes of the outer stream relative to the declared payload
  size. Add a contract test with a small gz bomb.

### RT-3 MEDIUM — The writer can produce an Archive the reader rejects (Manifest > 16 MiB, ~92k members)
- Where: `domain/archive_format.py:120` (`MANIFEST_MAX_BYTES`), read at `adapters/tar.py:755`; nothing in
  `collect_site_root`/`write_archive` checks it.
- Attack/chaos: a legitimate large site (media-heavy uploads, > ~92,000 files at ~183 B per member entry,
  more with an `excluded` list) makes a manifest over the cap.
- Evidence: `write_archive` over 110,000 empty files wrote a 22 MB Archive; `verify_archive` returned
  `passed=False`, reason `UNREADABLE` on `manifest.json`. The publish gate (`verify_or_raise`) then fails
  the backup after the whole payload (and, with encryption, the whole encrypt pass) was produced. Fail-closed,
  but a large site can never be backed up, and the failure is late and unexplained ("UNREADABLE").
- Reachable: yes, plain `backup --mode files|all` today.
- Fix: count members during `collect_site_root` and refuse early with a named error, or raise the cap with a
  streaming parse, or move the member index into its own sized member. Add a test at the boundary.

### RT-4 MEDIUM — An encrypted Archive still exposes a full inventory in clear, and user docs do not say so
- Where: ADR-0024 ("`manifest.json`: first and never encrypted"); README.md:57 and docs/restore.md speak of
  the Manifest only as *unauthenticated*, never as *unconfidential*.
- Attack: anyone who can read the Archive (object-store reader, backup operator, a leaked copy) learns the
  `source` (site root path, database name, origin), every file path and size (plugin/theme names and
  versions → targeted exploitation), exclusion patterns and which paths were excluded, without the
  passphrase. Evidence: `tar xOf encrypted-passphrase.tar.gz manifest.json` shows `source.site_root`,
  `db_name` and the member list in clear.
- Reachable: yes for any encrypted Archive (library API today).
- Fix: state it in README/`--encrypt-with-passphrase` help/docs/restore.md ("encryption protects file
  contents only; the file list and names are readable"). Optionally shrink the clear Manifest to what keyless
  verify needs and carry the member index only inside the payload (a design change; would need an ADR).

### RT-5 MEDIUM — No passphrase strength floor and no confirmation on entry
- Where: `cli/passphrase.py:100` uses `MIN_SECRET_LENGTH` (`adapters/redaction.py:61` = 4), a limit chosen
  for *redaction*, as the encryption floor. `terminal_prompt()` is one `getpass` call.
- Attack/abuse: a 4-character passphrase is accepted; the age scrypt wrap (database/age default work
  factor) is then brute-forceable offline from a copy of the Archive, which holds `wp-config.php` (DB
  credentials, salts) and the Dump. A typo at the single prompt makes the Archive unrecoverable and nothing
  catches it (the post-write verify is keyless; the restore passphrase is typed separately).
- Evidence: code read (`len(value) < 4` is the only check); not exercised by timing.
- Reachable: library API today (the CLI flag is unavailable); becomes CLI-reachable when the flag ships.
- Fix: raise the floor for *encryption* (separate from the redaction minimum; owner to pick, e.g. 12), warn
  or refuse low-entropy values, prompt twice on backup. Keep decryption permissive (an old Archive must stay
  readable).

### RT-6 LOW — Terminal-escape and forged-line injection through member paths / symlink targets
- Where: `domain/archive_format.py` `member_path` (accepts `\x1b`, `\n`; refuses only `\0`);
  `cli/restore.py:1580` prints `WARNING: symlink {link.path} -> {link.link_target} ...` raw to stderr.
- Attack: a plain Archive (or a hostile Manifest) with a symlink member named/targeted
  `x\x1b[2J...` or `a\nWARNING: ...` that the extraction filter skips; the operator's terminal executes the
  escape or shows an attacker-written line. Other messages use `repr` (safe); this one does not.
- Evidence: `member_path(SITE_ROOT, "a\x1b[2Jb")` and `"a\nb"` return a path; `\0` is refused. The print
  site read; the full restore not run.
- Reachable: yes (restore of an untrusted plain Archive that contains a skipped link).
- Fix: print with `repr()`/`!r` or strip control characters; consider refusing control characters in
  `_role_relative_ok`.

### RT-7 LOW — database's `cli` passphrase backend hands the whole environment to `age` and resolves it via a different PATH rule than preflight
- Where (third-party, `l3io-wordpress-database` 0.3.2, `_passphrase.py` `_cli_crypt`): `env = os.environ`
  minus `AGE_PASSPHRASE*`, plus `AGE_PASSPHRASE_FD`; binary from `shutil.which("age")`. This repo's
  `adapters/crypto.py` `passphrase_backend_requirements` and `adapters/preflight.py:236` ignore relative PATH
  entries, but the executor does not.
- Attack: with the `cli` backend (selected explicitly, or `auto` when the `l3io-age-passphrase` wheel is not
  installed — it is installed here, so the default is the in-process binder), the `age` child inherits
  `L3IO_WP_BACKUP_PASSPHRASE` and any DB-credential variables; a relative PATH entry (`.`, empty) lets a
  planted `age` in the working directory receive the passphrase through the fd.
- Evidence: source read; not run.
- Reachable: when the cli backend is in use. Passphrase never reaches argv (checked: fd pipe) — good.
- Fix: upstream — pass an allow-listed env (as `_client.py` already does), resolve `age` with the same
  relative-PATH rule as preflight; here, add a contract test that the backend is `binder` or that PATH has
  no relative entries.

### RT-8 LOW — Plaintext can survive a hard kill in the Run workspace; there is no stale-workspace sweep
- Where: `adapters/workspace.py` (`mkdtemp` in `$TMPDIR`, removed in `finally`); the plain payload and plain
  Dump exist there during backup (`tar.py` `write_archive`, deleted after use), and the decrypted payload
  during restore/authenticate. `cli/__init__.py:24-74` handles SIGTERM, so normal termination cleans.
- Attack/chaos: SIGKILL, OOM-kill or power loss leaves `l3io-wp-backup-*` holding the whole Site
  (including `wp-config.php`) and the Dump, or the decrypted payload, in `0700` dirs that nothing later
  removes. Confidentiality of "encrypted at rest" then depends on `/tmp` being tmpfs or cleaned.
- Evidence: code read; modes verified as `0700` dir, `0600` files (`_STAGED_MODE`, `O_EXCL|O_NOFOLLOW`).
  Output Archives are `0600`. ADR-0024 lists "plain data on disk" as an accepted trade-off, but docs/restore.md
  and the README do not tell the operator to point `TMPDIR` at tmpfs.
- Reachable: yes on any crash. Fix: sweep prior `l3io-wp-backup-*` dirs (owned by the user, older than the
  current boot or with no live lock) at start, and document `TMPDIR` on tmpfs.

### RT-9 OBSERVATION — `verify --authenticate` reads the Archive path twice
`cli/verify.py` runs `verify_archive_manifest(path)` and then `decrypt_payload` re-opens the path, with no
workspace copy (restore, by contrast, verifies a private `0600` copy: BL-E003-036). A swap between the two
reads can only substitute another genuine Archive under the same passphrase (the inner-Manifest compare fails
for any other Manifest), i.e. inside the documented rollback limit. No further action beyond noting it.

### RT-10 OBSERVATION — Exclusions are not bound to the CLI; the protected-path rule is sound
`docs/exclusions.md` says the default set takes effect with E008-S01-001; nothing excludes today. The default
set (cache, upgrade, debug.log) does not exclude secret-bearing files (`.env`, `.git`, `*.sql` dumps,
`error_log`, `.htpasswd`); that is a product choice, not a defect, but stays in plain Archives. Probed 12
patterns against `ExclusionSet` (`*`, `**`, `*.php`, `/wp-config.php`, `wp-*`, `!wp-config.php`,
`wp-config.p[h]p`, `**/wp-config.php`, `wp-config.php/`, `WP-CONFIG.PHP`, `wp-content/../wp-config.php`):
none removed `wp-config.php`. A pathological `**` pattern decided a 120-character path in 4 ms (no ReDoS seen).

### RT-11 OBSERVATION — Controls that held under attack
Run against a real encrypted fixture (`tests/fixtures/archives/v1/encrypted-passphrase.tar.gz`):
- outer Manifest `backup_scope` edited, sha256 unchanged: keyless `verify` passes (as documented);
  `--authenticate` → exit 8, `PayloadDamagedError`, names `backup_scope`, no value;
- 64 zero bytes appended to the age payload with Manifest sha256/size recomputed (the keyless-forgeable
  case): `--authenticate` → exit 8;
- age scrypt work factor forged to 2^40: rejected in 69 ms (no CPU/memory DoS);
- plain forged Archive: `--authenticate` refused (usage, exit 2);
- `encrypted:false` flip on an encrypted payload: `UNREADABLE`, exit 8;
- passphrase: never on argv (no flag; fd pipe to `age`), cipher `repr` redacted, `__slots__`,
  registry-registered before use, error messages carry no database text (`crypto.py`);
- extraction: hard links refused, duplicates refused, indexed-size and running-total caps, reserved
  inner-Manifest name never written, `os.path.ALLOW_MISSING` capability check for the June 2025 tarfile CVEs;
- `decrypt_payload` creates `0600` `O_EXCL|O_NOFOLLOW` and unlinks on any failure; `authenticate.payload`
  unlinked in `finally`.

### RT-12 OBSERVATION — Supply chain and AI poisoning
Runtime has no LLM component; AI-poisoning exposure is only in agent-authored artifacts (stories, review
files, the closure state), which no code executes. The committed v0 fixture `released-0.2.21-all` was built
from `wpconfigr`/`chesney` pulled from TestPyPI by exact version with `--no-deps` (owner decision A, noted in
6defdbe): a build-time dependency-confusion surface, not shipped; the fixture holds a 9-character throwaway
DB password and no salts. The fixture passphrase file is test-only and is guarded by
`tests/contracts/test_no_committed_age_key.py`.

## Not covered
Cross-platform behaviour (only Linux), the real MySQL load of the Dump (E003), the binder wheel's internals,
and an end-to-end `restore` run of the forged Archives (code-read for RT-1 and RT-6).

CRITICAL: 0, HIGH: 1, MEDIUM: 4, LOW: 3, OBSERVATION: 4
