# Close-out report (updated 2026-11-14)

**Status:** released and in production. Epics E002 to E008 are built and archived. Every backlog item is resolved except the 11 the owner deliberately deferred to after v1 (listed below). Owner decisions 1 to 89 are in [owner-decisions.md](owner-decisions.md).

## Releases

| Version | Date | What it brought |
| --- | --- | --- |
| 1.0.0 | 2026-11-10 | The first PyPI release; the family libraries became required dependencies (decision 76); the Docker image (GHCR and Docker Hub). |
| 1.1.0 | 2026-11-12 | The MariaDB-client image variant; `wpbak-<site>-<UTC>` names; `prune` with daily, weekly, monthly and yearly tiers; optional subfolders; `getenv_docker()` support; `--overwrite-wp-config` keeps `DB_*` source; the automation-first changes (decisions 47–86). |
| 1.1.1 | 2026-11-13 | The image never changes ownership and runs as `PUID`/`PGID` or `--user` (decision 87); payload gzip level 6, configurable (decision 88); `opentelemetry-api` ≥1.45.2 (GHSA-mf5v-w3w3-f6cg). |
| 1.1.2 | 2026-11-14 | `l3io-age-passphrase` ≥0.1.3 and `l3io-wordpress-database` ≥0.3.6 (fixed scrypt work factor; `WorkFactorTooHighError` instead of "damaged"); image builds wait for PyPI's CDN. |

**Family packages on PyPI:** `l3io-wordpress-config` 0.1.7, `l3io-age-passphrase` 0.1.3, `l3io-wordpress-database` 0.3.6. All four packages publish through PyPI trusted publishing; no long-lived PyPI token remains.

**Images:** `ghcr.io/liquidlogiclabs/l3io-wordpress-backup` and `docker.io/ravensorb/l3io-wordpress-backup`, amd64 and arm64.
- **Default (MySQL client):** tags `X.Y.Z`, `X.Y`, `X`, `latest`.
- **MariaDB client:** tags `…-mariadb`.
- **Gates on every image release:**
  - a scan (fixable CRITICAL or HIGH fails it), SBOM and provenance;
  - the sidecar suite on the pushed image;
  - the pushed image must equal the scanned one;
  - an anonymous-pull check.

## CI/CD

- **All four repos:** every workflow's latest run on every branch and tag is green (checked 2026-11-14). Secret scanning, push protection and Dependabot are on, with no open alerts.
- **Release gate:** `make preflight` runs the full local matrix under act. `make release` refuses without a `preflight` record for the exact commit and green GitHub runs for it. The other family repos have the same gate.
- **Hardening added during close-out:**
  - database jobs run only the integration tier; unit jobs use xdist;
  - the release job tolerates an existing release and checks its assets by sha256;
  - Docker Hub credentials no longer mask job outputs;
  - registry calls retry 429 and 5xx errors;
  - image builds wait for PyPI's CDN;
  - the runner's preinstalled MySQL server is purged before CI installs the client;
  - the sidecar test waits for WordPress to be ready and prints a failed step's stderr.

## Production (rwusdckrls6)

- **What runs where:** all 10 WordPress sites have a `wordpress-<site>-backup` sidecar (`1-mariadb`, Watchtower on, as of docker-apps-config-servers 559369a). Nightly runs are staggered from 03:00 to 04:30 UTC, with retention of 7 daily, 4 weekly and 6 monthly, and no encryption.
- **Tools containers:** 9 sites moved their `-tools` container to the official `wordpress:cli` image (uid 33, `/var/www/html`). adamlehrhaupt keeps its old tools container with the cron off (PHP 7.4, end of life).
- **Proof:**
  - backup, verify, scheduled mode and prune Dry Runs on prod;
  - restores of real prod Archives on the test host, identical table for table to a direct load of the dump;
  - no password in any log.
- **The repo isn't pushed yet:** docker-apps-config-servers commits are local, pending its local CI and confirmation of the first nightly runs.

## Left open by owner decision (11, after v1)

| Item | What |
| --- | --- |
| BL-E002-122 | Staged extraction for a root restore (openat/renameat). |
| BL-E003-041 | Journal the creation of the load database. |
| BL-E003-117 | A prune command for Safety Snapshots and saved configs. Archive prune shipped in 1.1.0. |
| BL-E006-024 | ADR-0025 option b: drivers in a neutral module. |
| BL-E006-025 | A public `open_run()` and `context=`, with BL-E006-024. |
| BL-E006-026 | A provider-neutral secret-command setting, replacing the AWS provider (decision 71). |
| BL-E007-002 | Replay real WP-CLI output as unit fixtures. |
| BL-E008-001 | Stream-parse the Manifest. |
| BL-E008-033 | Optional confinement of the relocation `wp` child. |
| BL-E008-034 | A read-only Manifest measurement before the dump. |
| BL-E008-035 | Streaming Manifest parse, then an on-disk member index. |

## Follow-ups in progress or pending

- **A patch in progress:** registry-retry for the publish and smoke steps, and Trivy scans no longer retried on real findings; an `--archive-subdir none` override; `prune --whatIf` listing the folders it would remove; specific errors for bad templates given by env or config; prune's own refusal wording when run as root.
- **To verify:** each site's first scheduled `run_finished` (03:00–04:30 UTC), and that the first week of prune keeps the right tiers.
- **For other owners:**
  - the blogs kit's `wp-site.sh` (busybox `find`, uid 33, new paths);
  - the database repo's clean-install check still resolving from TestPyPI;
  - foxchapelparksconservancy's backup tag should match the fleet.
- **Later:** clean up `/mnt/backups-general/wordpress/<site>` once two weeks of new backups exist.
