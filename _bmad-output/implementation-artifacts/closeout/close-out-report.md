# Close-out report (2026-11-08)

**Status: closed.** Epics E002 to E008 are built and archived. Every backlog item is resolved, except the 12 the owner deliberately left open (listed below). Owner decisions 1 to 46 are in [owner-decisions.md](owner-decisions.md).

## Verification (final tree, commit 5393ea5)

| Gate | Result |
|---|---|
| `make ci`, all jobs (act, Docker) | green |
| Unit and contract tests, Python 3.11 to 3.14 | 5486 passed each, 12 skipped |
| MySQL 8.0 leg as root, with integration tests | 5635 passed, 12 skipped |
| Other gates (see note) | pass |
| [test_closeout_gate.py](../../../tests/contracts/test_closeout_gate.py) | 7 passed |

The other gates are ruff, `mypy --strict`, lint-imports (4 contracts), the AD-1 purity check (27 modules), the AD-5 mode-defaults check (87 modules, 8 guarded types), the doc generators and `uv lock --check`.

## Close-out commits

| Commit | Content |
|---|---|
| 63a4c71 | Owner decisions 1 to 34 |
| ea7aef4 | The final Low batch, plus `l3io-wordpress-database` 0.3.3 |
| 1673328 | Owner decisions 35, 38 and 39 |
| a073c2e | Owner decisions 36, 37 and 40 to 44 |
| 49910dd | Owner decision 45 (docs) |
| 5393ea5 | Owner decision 46 |

Each code commit is followed by a `chore(close-out): resolve ...` commit that resolves its backlog items against that commit's SHA.

## Left open by owner decision (12)

| Item | When | What |
|---|---|---|
| BL-E006-022 | Before the first release | Claim the package names on PyPI. The family packages currently resolve from TestPyPI. |
| BL-E002-122 | Post-v1 | Staged extraction for a root restore (openat/renameat), then drop the race warning on `--allow-root`. |
| BL-E003-041 | Post-v1 | Journal the creation of the load database (a journal format addition). |
| BL-E003-117 | Post-v1 | A snapshot prune command: retention for Safety Snapshots and saved configs. |
| BL-E006-024 | Post-v1 | ADR-0025 option b: move the drivers out of `cli/` into a neutral module that the CLI and the library both call. |
| BL-E006-025 | Post-v1 | A public `open_run()` and the `context=` parameter, with BL-E006-024. |
| BL-E007-002 | Post-v1 | Replay real WP-CLI 2.12.0 output as unit fixtures. |
| BL-E008-001 | Post-v1 | Stream-parse the Manifest, as the 256 MiB cap is approached. |
| BL-E008-032 | Post-v1 | `--overwrite-wp-config`: keep the source text of `DB_*` expressions. This needs a new config library API. |
| BL-E008-033 | Post-v1 | Optional confinement of the relocation `wp` child. |
| BL-E008-034 | Post-v1 | A read-only measurement of the Manifest before the dump, so a Dry Run checks the 256 MiB cap. |
| BL-E008-035 | Post-v1 | Streaming Manifest parse, then an on-disk member index (with BL-E008-001). |

## Notes for whoever runs the tests next

- **Root-simulated test runs.** Since decision 44, the state-directory and sweep checks compare ownership with the effective uid. So a local root simulation that patches `os.geteuid` to 0 everywhere produces false failures in `adapters/workspace` and `test_state_directory`. Keep the real uid for those two. The real proof of the root path is the `make ci` leg, which runs as root.
- **Disk space.** Agents' pytest `--basetemp` directories under `~/.cache/pytest-bt` grew to 26 GB and filled the disk in the middle of a CI run. Delete them after each run.
- **The `main` branch is 117 commits ahead of `origin/main`.** Nothing has been pushed.
