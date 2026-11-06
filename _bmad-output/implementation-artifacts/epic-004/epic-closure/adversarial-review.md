# E004 adversarial + clean-release review

Scope: `git show 6f3c5c4` (S01-001), `6defdbe` (S01-002), `3a1d1eb` (S01-003). Review only; nothing changed.
ADR-0024 and its 2026-11-06 amendment were checked and not re-litigated. The code does not deviate from
them: the inner Manifest is last, appears once, is compared field by field through one shared function,
and the mismatch is `PayloadDamagedError(field=...)`, never a value.

Run: `ruff check src tests` is clean. 215 passed / 6 skipped over the new tests (exclusions, v0 fixtures,
crypto, interop, guards, encrypted backup). The skips are the `age` interop cases and one DB case. The
interop fixture fails instead of skipping when `CI` is set, so CI cannot skip it silently.
Not re-run: the RSS test in `tests/contracts/test_streaming.py`.

Prior reviews' "fixed" findings that I re-checked:
- S01-001 no-op patterns: `#c`, `/`, `[`, `[a`, `!` are refused as USAGE. Fixed.
- S01-003 extraction/read_member skip of the reserved name, and refusal of directory/duplicate/other members
  under it: present in `_ExtractFilter` and `_is_reserved`. Fixed.
- Not verified here: the S01-003 memory-test fix (the code reads sound, the test was not run).

## Findings

### AR-1 MEDIUM adversarial: the no-direct-crypto guard misses ordinary spellings
`tests/contracts/test_no_direct_crypto.py:22-50`. Reproduced by running `_offenders()` over a planted file:
only `from pyrage import x` was caught. Not caught: `from cryptography import hazmat` (module is
`cryptography`, not `cryptography.hazmat`), `from cryptography.fernet import Fernet` (a cipher outside
`hazmat`), `os.system('age -d')` and `os.popen`, and `asyncio.create_subprocess_exec('age')` (none are in
`PROCESS_CALLS`). The docstring says "a `subprocess` or `os` process call", which overstates what is
covered (only `os.exec*/spawn*/posix_spawn*`). Only `import_module` is listed as uncovered.
Fix: forbid the `cryptography` package as a whole (the repo has no use for it, see AR-4) and match `from X
import hazmat`; add `system`/`popen`/`create_subprocess_*` to `PROCESS_CALLS`; list the remaining gaps in
the docstring. Plant each of these in the planted-tree test.

### AR-2 MEDIUM clean-release: the `database` extra cannot resolve from PyPI
`pyproject.toml` (`database` extra, `l3io-age-passphrase>=0.1.1,<0.2`, plus `[tool.uv.sources]` testpypi).
`uv.lock` takes the binder from `test.pypi.org`. `[tool.uv.sources]` is not part of published metadata, so
`pip install l3io-wordpress-backup[database]` asks PyPI for a package the comment itself says "is not
published". The same holds for `l3io-wordpress-database` and `-config`, which are declared and accepted
(the comment says the revert condition is not met). This story adds a third such package and puts the
encrypt/decrypt path behind it. Not a defect in this commit, but it is a release blocker for the epic.
Fix: record it as an open release gate (publish to PyPI, or document the TestPyPI install) before any
tag. Check that `verify-install` in CI covers the extra from a clean index.

### AR-3 MEDIUM adversarial: the executed `age` and the preflight probe can differ
`adapters/crypto.py::passphrase_backend_requirements` restates database's backend choice and probes `age`
and `age-plugin-batchpass` through the preflight, which ignores relative PATH entries (E005 closure,
`adapters/preflight.py:231`). Database's `cli` backend (`_passphrase.py:151`, `_cli_crypt`) resolves
`shutil.which("age")` itself, which honours `.` and empty PATH entries, then runs it with the whole
inherited environment and the passphrase on a pipe FD. Under `L3IO_AGE_PASSPHRASE_BACKEND=cli`, or
`auto` without the binder, with `PATH=.:/usr/bin` and a planted `./age` in the Site Root (cwd), the
preflight passes on the real `age` while the planted one receives the passphrase and the whole payload
(the Dump). The child also inherits `L3IO_WP_BACKUP_PASSPHRASE` and any other env secret (cf. the
BL-E002-117 / O3 minimal-environment decision for E007). Database owns the code; this package owns the
gate. The default path (binder importable) is not affected.
Fix: in `check()` (or `required_tools`), refuse when `PATH` has a relative or empty entry and the
selected backend is `cli`; or resolve with the preflight's absolute `which` and pass that to database
once it exports a hook. File upstream against database for a minimal child env.

### AR-4 MEDIUM clean-release: `cryptography` is a hard dependency that nothing imports
`pyproject.toml:37` (`"cryptography>=45,<51"`) is in core `dependencies`. `grep` finds no `import
cryptography` in `src/`, `tools/` or `tests/` except the guard that forbids it. After ADR-0024 (all crypto
through database/pyrage) it is a dead native dependency in every install, with its own CVE surface, and it
makes AR-1's gap look intentional. Pre-existing, but this story's guard now proves it unused.
Fix: remove it from `dependencies` (and the lock) or record why it stays. If it is kept, extend the
import-linter contract so nothing may import it.

### AR-5 LOW adversarial: the restore reads the config out of the payload before the inner-Manifest check
`cli/restore.py:962` (`_archive_config`) decrypts the whole payload and reads `wp-config.php` while the
plan is built. The inner-Manifest binding runs later, in `restore_runner._verify`. A genuine payload from
another Archive under the same passphrase, swapped under this Manifest, is authentic to age, so its config
is parsed and used as the Site descriptor when the target has none, and only then does VERIFY refuse it.
Nothing destructive happens before the refusal, so this does not break the amendment, which says "before
the Safety Snapshot". It does mean unbound content is consumed early, and the payload is decrypted twice
(this path and VERIFY), which at scale is a full scrypt-and-decrypt pass twice.
Fix: run `check_inner_manifest` in `_archive_config` too, or decrypt once and hand the file to VERIFY.

### AR-6 LOW adversarial: extraction does not itself require the inner Manifest
`adapters/tar.py` `_ExtractFilter` (~1059) and `read_member` skip or refuse the reserved name, but
`extract_archive` and `read_member` never check that an encrypted payload's inner Manifest is present, last
and equal; they rely on the caller having run VERIFY. Today the only extraction caller is restore, after
VERIFY. A future caller (the `api/` library root, E006) that extracts an encrypted Archive without
`read_inner_manifest` skips the binding silently.
Fix: add a contract test that every call site of `extract_archive`/`decrypt_payload` in `src/` is paired
with `read_inner_manifest` (derive the scope from an AST walk), or enforce it inside `decrypt_payload`.

### AR-7 LOW adversarial: the committed-key guard misses post-quantum age identities
`tests/contracts/test_no_committed_age_key.py:20-21`. The regex requires `AGE-SECRET-KEY-1` plus 58
characters. age 1.3 (the version this story pins) also has `AGE-SECRET-KEY-PQ-1...` identities, which do
not match, so a committed PQ identity passes the guard. The planted-key test cannot show it because it plants
only the classic form. Fix: match `AGE-SECRET-KEY-(PQ-)?1[0-9A-Z]{58,}` and plant the PQ form.

### AR-8 LOW adversarial: a plaintext-archive restore is gated on the encryption backend
`cli/restore.py:1659-1681` builds the cipher (and runs `check()`) whenever a passphrase resolves, and passes
`cipher.required_tools()` as `extra_tools` without looking at the Archive. `core/restore.py:1493` merges them
unconditionally. A restore of a plain Archive with `L3IO_WP_BACKUP_PASSPHRASE` set in the environment (or an
invalid `L3IO_AGE_PASSPHRASE_BACKEND`, or `cli` without `age`) is refused for a backend it never uses.
Fix: require the tools and run `check()` only when `view.encrypted`.

### AR-9 LOW clean-release: "GENERATED" markers with no generator
`docs/exclusions.md:25,32` calls the table generated, but nothing generates it (`tools/`, Makefile, CI have
no reference). It is hand-maintained and only checked by `test_exclusions_documented.py`. Fix: rename the
markers to "CHECKED" or add the generator.

### AR-10 LOW adversarial: the wp-config protection is case-sensitive
`core/exclusions.py` `_protects`/`PROTECTED_PATHS`. `decide(PurePosixPath("WP-CONFIG.PHP"))` under `*`
returns `excluded=True`. On a case-insensitive filesystem that is the same file. The docs say matching is
case-sensitive, so this is documented and Linux-only today. Fix: casefold the protected comparison, or state
the Linux-only assumption in `PROTECTED_PATHS`.

### AR-11 LOW clean-release: generic credential in a binary fixture
`tests/data/wp-config.php` carries `DB_PASSWORD 'pass@word1'`; it is copied into
`v0/released-0.2.21-all.tar.gz` and the other v0 fixtures. It is a throwaway container's password, but it
is not a unique sentinel, so a secret scanner or the `sentinel-pw-` hygiene scan cannot tell it from a real
leak. `test_the_released_fixture_carries_no_operator_secret` only proves byte equality with the test
config. Fix: use a unique marker such as `fixture-only-db-password-not-a-secret` (regenerate the fixtures).

### AR-12 LOW clean-release: unreachable branches kept under `pragma: no cover`
`adapters/restore_runner.py:598` (`if text is None: raise PayloadDamagedError`) is unreachable by the
comment's own account. In `cli/verify.py` the same `manifest_text is None` is folded into
`ArchiveNotEncryptedError`, which is a USAGE error with a misleading message for a case that cannot happen.
Fix: assert, or restructure `verify_archive_manifest` to return the text with the view so the `None` is
unrepresentable.

## Checked and found sound (no finding)
- Exclusions use `pathspec` (no hand-rolled glob); the wp-config rule holds against `*`, `**`, `wp-config.*`,
  `!` sequences and a directory pattern; escaped logging of paths.
- Crypto goes through database; no argv carries a passphrase (Popen spy test, FD pipe); the encrypted fixture
  passphrase is a labelled sentinel; no `TODO`/`FIXME`/`print` debug artifacts in the changed `src/` apart
  from existing CLI output.
- v0 reading needed no `src` change; fixture selection is one rule (`archive_files`), scope asserted
  non-empty, a stray directory is caught.
- Downgrade (an encrypted Archive rewritten as plain) is a documented non-goal (ADR-0016 amendment); the
  restore warns that a plain Archive cannot be authenticated (`cli/restore.py:233`). The rollback limit is
  stated in `--authenticate` output, help and docs.

CRITICAL: 0, HIGH: 0, MEDIUM: 4, LOW: 8
