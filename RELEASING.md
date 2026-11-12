# Releasing

Three packages publish together and **must be released in dependency order**.
Ordering is manual and deliberate: the gate that matters — "does the published
artifact actually resolve and import *from the index*?" — can only be checked
after the upstream package is live, so automating the chain would only automate
guessing.

```
l3io-wordpress-config  ->  l3io-wp-database  ->  l3io-wordpress-backup
```

All three share the PEP 420 namespace `l3io.wp` (ADR-0012, AD-27).

| # | Distribution | Import | Repository |
|---|--------------|--------|------------|
| 1 | `l3io-wordpress-config` | `l3io.wp.config` | `l3io-wordpress-config` |
| 2 | `l3io-wordpress-database` | `l3io.wp.database` | `l3io-wordpress-database` |
| 3 | `l3io-wordpress-backup` | `l3io.wp.backup` | `l3io-wordpress-backup` |

## The gate

After each release, before starting the next:

```bash
uv run --isolated --no-project --with l3io-wordpress-config==<ver> \
       python -c "from l3io.wp import config; print('ok')"
```

Run it against **PyPI**, not a local build. Installing the working tree would
have passed throughout the entire period when the published package could not
resolve at all — that blind spot is what let the original outage ship, and this
ordering exists to close it.

`l3io-wp-database` additionally must not pull `boto3` in a base install (AD-8).
`l3io-wordpress-backup`'s release pipeline runs the cross-package namespace check
(AD-27): all three wheels installed into one interpreter, all three imported,
and no `__init__.py` at either namespace level.

## The rule: green CI and a local preflight, every time

**Family rule (Shawn, 2026-11-08): nothing is released, tagged or published until
CI is fully green on GitHub and `make preflight` has passed locally for that
commit.** It is enforced, not remembered:

- `make preflight` refuses a modified tree, runs `lint`, `types` and `contracts`,
  then `make ci` (every job and leg act can run, `security-audit.yml`, and the
  `publish.yml` / `release.yml` dry runs). Only a full pass records HEAD's SHA, in
  `.git/l3io-preflight`.
- `make release` (and `release-patch|minor|major|dev`) runs
  `scripts/release-gate.py` before dispatching. It refuses when the record is not
  HEAD, when GitHub has no runs for HEAD's full SHA, when any run is not
  `completed`/`success`, or when a workflow that runs on a push to `main` (derived
  from the workflow files) has no run. Each refusal is proved by a planted failure
  in `tests/contracts/test_release_gate.py`.
- Not covered: dispatching `release.yml` by hand, or pushing a tag directly,
  bypasses both checks.

## Per-package steps

1. `make preflight`, push, and let CI go green; `make release` checks both.
2. Bump the version in `pyproject.toml`; update the changelog.
3. `make release-<bump>` tags `vX.Y.Z` and dispatches `publish.yml` on the tag
   with `target=mirror`: build, the clean-install smoke matrix, the namespace
   check, the internal mirror, and the GitHub release with the wheel and sdist.
   Then publish to PyPI (trusted publishing) from the same tag:
   `gh workflow run publish.yml --ref vX.Y.Z -f target=pypi`. That run finds
   the release already there: `create the release` skips creating it
   (`skip-if-release-exists`) and checks its assets against this run's build.
   It uploads a missing one and fails, uploading nothing, when a same-named
   asset's sha256 differs. The build is reproducible, so they match.
   Asserted in `tests/contracts/test_workflow_invariants.py`.
4. Run the gate above.
5. **The Docker image** (`l3io-wordpress-backup` only; owner decisions 77–81).
   `docker.yml` starts by itself when a Publish run's `publish to PyPI` job
   succeeds: it waits until the version installs from PyPI, then for each
   variant (the default, MySQL client; `-mariadb`, MariaDB client) builds both
   platforms from that release, scans them, pushes by digest, runs the sidecar
   suite against the pushed digest on the engines that variant is for, then
   tags and attests: `X.Y.Z` and `X.Y.Z-mariadb`; `X.Y`, `X`, `latest` and
   `X.Y-mariadb`, `X-mariadb`, `mariadb` when it is PyPI's latest. Check that
   every leg of that run is green; a failed variant publishes neither.
   To build an existing release (1.0.0, the first image) or rebuild one (a new
   base, a fixed CVE): `gh workflow run docker.yml -f version=X.Y.Z`.
   [docs/docker.md](docs/docker.md) has the details.

Rehearse without publishing: `workflow_dispatch` with `target: testpypi` (it
builds no image: the PyPI job does not run).

### The image's registries (one-off setup)

The variables and their defaults are in [docs/docker.md](docs/docker.md#release-pipeline)
(the family's `REGISTRY_NAME`/`REGISTRY_ORG`/`IMAGE_NAME` convention, plus
`DOCKERHUB_ENABLED`, default `true`).

- **GHCR** (`ghcr.io/liquidlogiclabs/l3io-wordpress-backup`): nothing to
  configure; `GITHUB_TOKEN` pushes. **After the first push the package is
  private**, whatever the repository's visibility (a linked package inherits
  its repository's access permissions, not its visibility). That first run
  fails at "every published tag pulls anonymously". Make the package public
  once, as an organisation owner: the package's page -> Package settings ->
  Danger Zone -> Change visibility -> Public (irreversible; GitHub documents no
  API for it). Then re-run the failed job; the tags are already pushed.
- **Docker Hub** (`docker.io/ravensorb/l3io-wordpress-backup`, public; the
  default `DOCKERHUB_ORG` is `ravensorb`): the repository **variable**
  `DOCKERHUB_USERNAME` and the repository **secret** `DOCKERHUB_TOKEN` (an
  access token with read/write scope on that repository). The username is not
  a secret and must not be one: it equals the org, so as a secret it masks the
  image name and GitHub drops the `names` output (run 43001741839). With
  `DOCKERHUB_ENABLED` true (the default) and either missing, the run fails in
  `credentials`, before building; `DOCKERHUB_ENABLED=false` pushes to GHCR only.
- Never set `ACT_DRYRUN` or `LOCAL_ACT` as GitHub variables: they exist for act.

## Legacy names

`wpbackup2` and `wpdatabase2` are owned by this project and currently **fail to
install** — 0.2.21 requires `chesney` and `wpconfigr`, both removed from PyPI, so
`pip install wpbackup2` ends in `ResolutionImpossible`.

Each gets **one final pointer release** whose only dependency is its `l3io-wp-*`
replacement, emitting a `DeprecationWarning` on import that names the successor.
PyPI has no rename, redirect or alias mechanism, so this is the only migration
path available — and it repairs the broken install for everyone still on the old
name. Publish these *after* the corresponding `l3io-wp-*` package is live.

`wpbackup`, `wpdatabase` and `wpconfigr` are registered as thin pointer packages:
they are the names a stranger searches and the names this project's own history
references, and leaving them unregistered leaves them open to revival hijack.

## Why there is no cross-repo automation

PyPI's trusted publishing matches the *called* workflow's filename, so the publish
job cannot live in a reusable workflow shared across repositories. Comparable
multi-package Python projects that release in dependency order — Pallets, and
OpenTelemetry Python's core/contrib split — document the ordering rather than
automate it.

## Local replay

Everything except publishing runs locally:

```bash
make act-init             # first time only — creates .act.{vars,env,secrets}
make secrets-check        # confirms Infisical resolves for this environment
make ci                   # every workflow act can run, full matrix
make ci-quick             # ci.yml with Python 3.11 across every engine
make ci-job JOB=contracts # one job
make preflight            # the pre-release run; records the commit it passed for
```

The Makefile header lists exactly what `make ci` runs and what stays
GitHub-only, with the reason for each.

**Two test tiers (2026-11-09).** `ci.yml`'s database legs (every Python × every
engine) run only `pytest -m integration`; the `unit+contracts` legs (every
Python) run `-m "not integration"`. The two selections are complements on the
same Pythons, so the whole suite still runs on every interpreter and every
integration test on every engine. Three checks keep the split exact:

- `tests/contracts/test_integration_marker_reach.py` fails if a test that needs
  the database is not marked `integration` (it would run in no leg). "Needs the
  database" is derived from DB fixtures, `DB_HOST`/`DB_PASSWORD` reads and
  skips that depend on `DB_HOST`. It is not a hand-kept list.
- `tests/conftest.py`: with `CI` set, an `integration` test that skips fails.
  The exceptions are a skip raised as `EngineUnsupported` (for example MariaDB
  has no runtime TLS reload) and a `root` test on a runner that is not root.
- `tests/contracts/test_workflow_invariants.py` (with
  `scripts/check-cicd-alignment.py`'s `check_test_tiers`): each tier selects
  exactly its marker expression with no further narrowing, on equal Pythons.

Coverage is measured in both tiers and reported once per leg. There is no
combined percentage, because that would need the two jobs' data files in one
place, and act cannot run `upload-artifact@v7`. No threshold is set.

Secrets come from Infisical: `make ci` runs act under `infisical run`, which exports
them as environment variables — which is how act resolves the bare `-s NAME`
declarations in `~/.actrc`. Nothing secret is written to disk in this repository.
`INFISICAL_ENV` and `INFISICAL_PROJECT_ID` are taken from your environment when set.

`publish.yml` and `release.yml` are only dry-run under act: act has no OIDC and
ignores `job.permissions`, `publish.yml` hands its wheel over with
upload-artifact v7, which act's artifact server does not speak, and
`release.yml` creates tags.
