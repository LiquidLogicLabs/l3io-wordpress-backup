# Closure report: epic E004 (combined sprint 01 + epic closure)

Goal: FR-5 path exclusions, reading pre-rewrite (v0) Archives, and encryption at rest (passphrase-only, inner-Manifest binding). Status: done; archived.

Reviews (one pass each over the whole epic): `adversarial-review.md` (0 critical, 0 high, 4 medium, 8 low), `redteam-report.md` (0 critical, 1 high, 4 medium, 3 low, 4 observation), `arch-drift-review.md` (0 blocker, 1 major, 5 minor), `retrospective.md`. Sprint-level pointers are in `epic-004/sprint-01/closure/`.

## Fixed in this closure (8)
| Finding | Fix | Regression test (fails without fix) |
|---|---|---|
| RT-1 HIGH plain-archive substitution accepted by restore | `restore --require-encrypted` (exit 2, `ArchiveNotEncryptedError`); docs/restore.md; generated CLI docs | `tests/unit/cli/test_restore_root.py::test_require_encrypted_refuses_a_plain_archive_before_any_effect` |
| RT-2 MEDIUM tar header-body memory bomb | `_BoundedTarInfo` refuses L/K/x/g/X header over 1 MiB before reading the body (`adapters/tar.py`) | `tests/adapters/test_tar_extended_header_bound.py` (270 MB peak without fix) |
| RT-4 MEDIUM Manifest readable in clear | README and docs/restore.md state it | docs |
| AR-3 / RT-7 MEDIUM planted `age` via relative PATH | `PassphraseCipher.check()` refuses the `cli` backend with an empty or relative PATH entry | `tests/adapters/test_crypto.py` |
| AR-1 MEDIUM guard misses spellings | `test_no_direct_crypto.py` hardened and planted | the guard's planted-tree test (old guard returned no offenders) |
| AR-7 PQ age identities | regex and plant | `test_no_committed_age_key.py` |
| AD-3 stale docstring, AD-4 factory-only construction | `ports/crypto.py`; new `test_cipher_built_only_by_factory.py` | the new contract |

Integration exposure: the restore CLI (RT-1) and the shared tar reader (RT-2) are exercised by `tests/integration` restore, verify and backup tests; the `cli` age backend (AR-3) by none. Full suite run at closure against MySQL 8.0 (mysql:8.0 container, client binaries from the image): 2964 passed, 10 skipped (age 1.3 interop skips on this host; CI installs 1.3.2). One earlier full run had a single transient failure of the RSS test `test_streaming.py::...[encrypted]`; it passed alone and on the rerun (flake under load, recorded).

## Deferred (18 backlog items)
AD-1 (MAJOR, FR-5 reach) as spec-proposal BL-E004-069 and an AC in E008-S01-001; RT-5 BL-E004-052 (E008-S01-001 AC); AR-6 BL-E004-060 (E006-S01-001 AC); RT-7 residual BL-E004-055 (E007 BL-E002-117 covers the child env); the rest are Low: BL-E004-054, 056, 059, 061 to 068; Medium without a code owner: BL-E004-053, 057, 058 (owner decisions below).

## Owner decisions (5), batched
1. **RT-1 default.** `--require-encrypted` is opt-in. Options: (a) keep opt-in; (b) refuse a plain Archive whenever a passphrase was supplied explicitly, with `--allow-unencrypted` to override; (c) always refuse unless `--allow-unencrypted`. Recommend (b). It changes the ADR-0016 amendment's waiver, so it is yours.
2. **RT-3 / BL-E004-053.** Manifest over 16 MiB (about 92k members) makes a large site un-backable, late. Options: refuse early with a named error; raise the cap with a streaming parse; move the member index out of the Manifest. Recommend a named early refusal now and the streaming parse in E006.
3. **AR-2 / BL-E004-057.** `l3io-age-passphrase`, `l3io-wordpress-database` and `-config` come from TestPyPI; `pip install ...[database]` fails from PyPI. Release gate: publish them or document the index. Recommend a gate entry in `docs/closeout-gate.yaml`.
4. **AR-4 / BL-E004-058.** `cryptography` is a core dependency nothing imports; removing it needs a `pyproject.toml`/`uv.lock` change, barred here. Recommend removal.
5. **RT-5 value.** Encryption passphrase floor (suggest 12) and double prompt; to be set in E008-S01-001 (BL-E004-052).

No orchestrator decision was reversed. ADR-0024 and its amendment: code does not deviate (all three lenses).

## Estimate vs actual
See the epic node (`pm-status show`); counterfactual man_hours formed before reading estimates.
