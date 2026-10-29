# Architecture Decision Records

Decisions for the wpbackup2 family — `wpbackup2`, `wpdatabase2`, `wpconfigr` — released together.

Governed by the architecture spine at
`_bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md`,
which carries the invariants (`AD-n`). An ADR here records a *mechanism*; an `AD` in the spine records an *invariant*.

| ADR | Decision | Status | Spine |
|---|---|---|---|
| [0001](0001-ports-and-adapters-functional-core.md) | Ports and adapters with a functional core | Proposed | AD-1, AD-4 |
| [0002](0002-publish-wpconfigr-existing-name.md) | ~~Publish `wpconfigr` under its existing name~~ | *Superseded by 0012* | AD-2, AD-15 |
| [0003](0003-credential-transport-option-file.md) | ~~Credentials by `--defaults-file`, never argv or `MYSQL_PWD`~~ | *Superseded by 0015* | AD-7 |
| [0004](0004-pymysql-driver.md) | PyMySQL as the MySQL driver | Proposed | AD-2 |
| [0005](0005-relocation-via-wp-cli.md) | Delegate serialization-safe URL rewriting to WP-CLI | Proposed | AD-13, AD-17 |
| [0006](0006-archive-format-and-encryption.md) | ~~Unencrypted manifest member; chunked AES-GCM under Argon2id, key-committed~~ | *Superseded by 0014* | AD-6, AD-11, AD-21 |
| [0007](0007-packaging-uv-trusted-publishing.md) | uv, PEP 621, trusted publishing across three repos | Proposed | AD-15 |
| [0008](0008-boto3-optional-extra.md) | `boto3` optional via lazy import | Proposed | AD-8 |
| [0009](0009-python-floor-3-11.md) | Python floor of 3.11 | Proposed | — |
| [0013](0013-ci-cd-local-replay-and-secrets.md) | GitHub Actions CI replayable under act; secrets from Infisical | **Accepted** | AD-1, AD-15, AD-27 |
| [0012](0012-package-naming-l3io-wp-namespace.md) | Publish as the `l3io-wp-*` family under a PEP 420 namespace | **Accepted** | AD-2, AD-15, AD-27 |
| [0011](0011-observability-structlog-plus-api-only-tracing.md) | structlog for logs; tracing API-only and inert unless a host configures it | Proposed | AD-10, AD-24 |
| [0010](0010-wpconfigr-regex-parser-scope.md) | Keep the regex `wp-config.php` reader, with documented limits and whole-file read-back | Proposed | AD-2, AD-18 |
| [0014](0014-age-encryption-via-database.md) | Archives are `age`, through `l3io-wp-database` (passphrase via binder or `age` CLI, amended 2026-10-27) | **Accepted** | AD-6, AD-21, AD-26 |
| [0015](0015-credentials-delegated-to-database.md) | Credentials reach MySQL client tools only through `l3io-wp-database` (credential construction only in adapters, `load` with `replace=None`, amended 2026-10-27) | **Accepted** | AD-7, AD-14, AD-29 |
| [0016](0016-run-context-binds-adapters-once.md) | The Run Context is opened without adapters and binds them exactly once | **Accepted** | AD-16, AD-19 |
| [0017](0017-manifest-v1-member-index.md) | The v1 Manifest member index, and `SUPPORTED_FORMAT_VERSIONS` tested against the fixtures | **Accepted** | AD-6, AD-11, AD-23 |
| [0018](0018-redaction-at-the-boundary.md) | Redaction is applied where text leaves the program, not inside domain errors | **Accepted** | AD-1, AD-7, AD-14 |
| [0019](0019-backup-dry-run-scope.md) | What a backup Dry Run executes | **Accepted** | AD-4 |
| [0020](0020-restore-undo-derived-at-read.md) | Restore undo text is derived when the journal is read | **Accepted** | AD-18 |
| [0021](0021-run-journal-location-lifecycle-resolution.md) | Run Journal location (per-Site state directory), lifecycle and resolution | **Accepted** | AD-18 |
| [0022](0022-restore-planned-from-probe-executor-runs-every-phase.md) | A restore is planned from a probe; the executor runs every phase, in a Dry Run too | **Accepted** | AD-4, AD-13, AD-17 |
| [0023](0023-target-wp-config-kept-unless-overwrite.md) | The target's `wp-config.php` is kept unless `--overwrite-wp-config` | **Accepted** | AD-18, AD-22 |
| [0024](0024-one-encrypted-payload-passphrase-only.md) | One encrypted payload per Archive, passphrase only for v1 | **Accepted** | AD-6, AD-11, AD-21, AD-26 |

The spine carries 30 invariants (AD-1..AD-30). AD-16..AD-21 came from the reviewer gate: Run Context, phase ordering, recoverability, Site Lock, atomic publication, streaming. AD-22..AD-24 came from the rewrite mandate: the CLI as a frozen contract, archive-format detection with permanent v0 reads, and API-only tracing. AD-25..AD-27 close the flag-to-mode map, the passphrase chain, and the PEP 420 namespace discipline. AD-28 and AD-29 came from the database work (GRANT escaping, administrator-answered existence); AD-30 records the git tag as the only version.

New ADRs take the next number from `pm-status.py adr-reserve`, which keeps `_bmad-output/implementation-artifacts/state/adr-register.yaml` under a lock.
