# The official Docker image

`wpbackup2` as a sidecar next to the official `wordpress` image (owner decisions
77–80). The README's [Run as a Docker sidecar](../README.md#run-as-a-docker-sidecar)
has the compose example; this page is the reference.

```
ghcr.io/liquidlogiclabs/l3io-wordpress-backup:<tag>
docker.io/liquidlogiclabs/l3io-wordpress-backup:<tag>
```

Both public, the same digest in each.

## What is in it

| Part | Version | How it is pinned |
|---|---|---|
| Base | `ghcr.io/linuxserver/baseimage-ubuntu:resolute` (Ubuntu 26.04, s6-overlay) | by digest (`BASE_IMAGE` build-arg) |
| `l3io-wordpress-backup` | the PyPI release named by `VERSION` | `==VERSION`, asserted after the install |
| Python | Ubuntu 26.04's `python3` (3.14) | the base |
| Dump/load client | Ubuntu's `mysql-client` 8.4 | the base's archive |
| `age`, `age-plugin-batchpass` | 1.3.2, built with Go 1.27.4 | module hash `h1:r6RSZ…` asserted; Go image by digest |
| Supercronic | 0.2.51 | SHA-256 per architecture (amd64, arm64) |

`linux/amd64` and `linux/arm64`. Built from the release **on PyPI**, never from a
checkout ([.github/workflows/docker.yml](../.github/workflows/docker.yml)); the
Dockerfile's `wheels` build context is for `tests/sidecar/run.sh` only, and
`tests/contracts/test_docker_image.py` asserts the release workflow never passes it.

**`age` from source, not the release binaries.** age 1.3.2's release binaries
are built with Go 1.27.0, whose standard library has four fixed HIGH advisories
(CVE-2026-78667, -78669, -97031, -111943), which the scan policy below fails on.
The same tagged module built with Go 1.27.4 has none. The `age` CLI is only the
passphrase backend's fallback: the `l3io-age-passphrase` binder is installed, so
`L3IO_AGE_PASSPHRASE_BACKEND=auto` (the image's setting) uses it.

**No WP-CLI.** Only a URL relocation (`restore --new-site-url` /
`--new-site-home-url`) runs `wp`; backups and restores, `--archive latest`
included, never do. Carrying it costs PHP with `mysqli` (+27.9 MB, measured on
this base, plus the 7 MB phar), the PHP CVE surface in the scan, and running the
Site's own PHP inside the backup container. Without it such a restore stops
before any change, exit 7 `NOT_READY`: "wp is needed for SEARCH_REPLACE ...;
found: absent" (asserted by `tests/sidecar/run.sh`, step
`B-relocation-needs-wp-cli`). Relocate from a host with WP-CLI, or extend the
image (`FROM` it, install `php-cli php-mysql` and a checksummed `wp-cli.phar`).

**No HEALTHCHECK.** A one-shot run has nothing to probe. In the scheduled mode s6
restarts Supercronic if it exits, and a failed job is in the container log with
its exit status, which a container health state would not say more clearly.

## One image for MySQL and MariaDB

One image, with MySQL's 8.4 client. Proven with the full sidecar suite
(`tests/sidecar/run.sh`, every asserted step and check) against:

| Server | MySQL 8.4 client (the image) | MariaDB 11.8 client |
|---|---|---|
| `mysql:8.0` | passes | **fails**: the first backup's dump exits 2 (`client_server_skew`: a MariaDB `mysqldump` against MySQL 8.0) |
| `mysql:8.4` | passes | not run (the 8.0 failure decided it) |
| `mariadb:11.4` | passes (`mysqldump` with `--column-statistics=0`, l3io-wordpress-database 0.3.5) | passes |

So no per-family tags. `DB_CLIENT=mariadb` still builds a MariaDB-client image
for anyone who wants one (`docker buildx build --build-arg DB_CLIENT=mariadb`);
it is not published.

## Two modes

**Scheduled (the default command).** The base's s6 init starts;
`init-adduser` moves the `abc` user to `PUID:PGID`; `init-wpbackup-config`
gives the image's writable directories to it and builds the crontab;
`svc-wpbackup-schedule` runs Supercronic as `abc`. Every job's output goes to
the container log unchanged (`-passthrough-logs`). A missing or unparsable
schedule stops the container at start (`S6_BEHAVIOUR_IF_STAGE2_FAILS=2`).

| Variable | Default | Meaning |
|---|---|---|
| `WPBACKUP_SCHEDULE` | `0 3 * * *` | one cron expression for `wpbackup2 backup`; empty to schedule only the file below |
| `WPBACKUP_CRONTAB` | `/etc/l3io-wp-backup/crontab` | a crontab appended as written, when it exists (named explicitly, it must exist) |
| `TZ` | UTC | the schedule's time zone |

The image's own variables are `WPBACKUP_*`, not `L3IO_WP_BACKUP_*`: `wpbackup2`
reads every `L3IO_WP_BACKUP_*` variable as a setting and logs WARNING
`unknown_setting_variable` on every Run for one it does not know.

Supercronic does not start a job again while its previous run is still going,
and the Site Lock refuses a second Run on the same site anyway. Avoid an
impossible date (`0 0 31 2 *`): Supercronic 0.2.51 accepts it and then logs a
warning in a busy loop.

**One-shot (any other command).** `docker run IMAGE backup` is `wpbackup2
backup`; `docker run IMAGE wpbackup2 backup` is the same. The first argument
runs as a command only when it names an executable on PATH (`sh`, `sleep`);
`tests/sidecar/run.sh` checks that no `wpbackup2` subcommand does. No s6 starts.

## Users

The image starts as root, as LinuxServer images do, and never runs `wpbackup2`
as root unless told to:

| Started as | Runs as |
|---|---|
| root (default), `PUID`/`PGID` unset | 33:33, www-data in the official `wordpress` image |
| root, `PUID=1000 PGID=1000` | 1000:1000; the image's state, workspace and home directories, and the top of `/backups`, are given to it first |
| root, `PUID=0` | root; `wpbackup2` then runs each backup and restore as the Site Root's owner itself (owner decisions 58, 67) |
| `user: "33:33"` (one-shot only) | 33:33; a read-only root filesystem works (below) |

`docker compose exec backup wpbackup2 ...` into a scheduled container runs as
root (the container's user) unless `-u 33` is given; `wpbackup2` then reads its
credentials as root and switches to the Site Root's owner, as above.

## Writable paths

| Path | What | Mount |
|---|---|---|
| `/backups` (`L3IO_WP_BACKUP_ARCHIVE_DIR`) | the Archives | a volume |
| `/var/lib/wpbackup/state` (`XDG_STATE_HOME`) | the Site Lock, the Run Journal, default Safety Snapshots | a volume (it must survive the container for an interrupted Run to be resolved) |
| `/tmp/wpbackup` (`TMPDIR`) | the Run's workspace, holding the plaintext Dump during a Run | a tmpfs: `mode=0700,uid=33,gid=33` |
| `/var/www/html` (`L3IO_WP_BACKUP_WP_DIR`) | the Site | the `wordpress` container's volume |

A one-shot container started as `user: "33:33"` runs with `read_only: true`
given those mounts (`tests/sidecar/compose.yaml`'s `backup` service). The
scheduled mode needs a writable root: the base's init rewrites `/etc/passwd`
for `PUID`/`PGID` and s6 writes under `/run`.

## Secrets

- **The database:** the same `WORDPRESS_DB_*` the `wordpress` container gets,
  including `WORDPRESS_DB_PASSWORD_FILE` for a Docker secret: `wpbackup2`
  resolves the official image's `getenv_docker()` `wp-config.php` as PHP does.
- **The passphrase:** `L3IO_WP_BACKUP_PASSPHRASE`, or a config file given as a
  secret and named by `L3IO_WP_BACKUP_CONFIG`. A config file holding a secret
  must be readable by its owner only: on the host, `chown 33:33` and
  `chmod 600` it (Compose bind-mounts a file secret with the host's owner and
  mode). Checked: the same file at `0644` is refused, exit 2.

## Tags

`X.Y.Z` on every build; `X.Y`, `X` and `latest` only when `X.Y.Z` is PyPI's latest
release, so rebuilding an older release never moves them backwards. One build,
one digest, pushed to every enabled registry.

## Release pipeline

[.github/workflows/docker.yml](../.github/workflows/docker.yml), after
`publish.yml` has published to PyPI (or dispatched with `version`):

1. **resolve**: the Publish run's `publish to PyPI` job succeeded (else nothing
   is built); the version installs from PyPI (waits up to 30 min); the
   registries, each with its credentials (below).
2. **build**: each platform built and scanned (Trivy, below), then both pushed
   **by digest only**, with an SBOM and `mode=max` provenance.
3. **smoke**: `tests/sidecar/run.sh` against that digest, pulled from the
   registry, on `mysql:8.0`, `mysql:8.4` and `mariadb:11.4`.
4. **publish**: the tags applied to the tested digest in every registry; a
   GitHub build-provenance attestation (Sigstore) pushed beside it; then every
   published tag must pull anonymously (the image is public, owner decision 80).

**Registries**, configured as the family's image releases are
(`LiquidLogicLabs/markdown-mermaidjs-to-pdf`'s `release.yml`), by repository
variables with these defaults:

| Variable | Default | Meaning |
|---|---|---|
| `REGISTRY_NAME`, `REGISTRY_ORG`, `IMAGE_NAME` | `ghcr.io`, `liquidlogiclabs`, `l3io-wordpress-backup` | the primary registry, always pushed; logs in with the secrets `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` when set, else `GITHUB_TOKEN` |
| `DOCKERHUB_ENABLED` | `true` | also push `docker.io/<DOCKERHUB_ORG>/<IMAGE_NAME>`, with the secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`; enabled without both, `resolve` fails before building |
| `DOCKERHUB_ORG` | `REGISTRY_ORG` | the Docker Hub namespace |
| `DOCKER_FILE`, `DOCKER_PLATFORMS` | `docker/Dockerfile`, `linux/amd64,linux/arm64` | |
| `IMAGE_BUILD_MODE` | `push` | `load`: build and scan, push nothing |

A new GHCR package is private and does not take the visibility of the
repository it is linked to (GitHub's package documentation: a linked package
inherits "the access permissions (but not the visibility)"). So the first run
fails at the anonymous-pull check until an owner makes the package public
(RELEASING.md); every later run checks it still is.

**Scan policy:** Trivy, run from its image pinned by digest (no third-party
action), over both platforms before anything is pushed. A **fixable** CRITICAL or
HIGH fails the run; every CRITICAL and HIGH, fixed or not, is listed. The image
upgrades the base's packages at build time, so a fix published after the base was
built is taken. Measured 2026-11-10 on the 1.0.0 image (amd64): 0 CRITICAL, 0
HIGH, fixed or not.

**Action pins:** every `uses:` in `docker.yml` is a full commit SHA with its
release in a comment; `tests/contracts/test_docker_image.py` asserts the form and
`scripts/check-cicd-alignment.py` (online) that each comment's tag is that commit.
The older workflows (`ci.yml`, `publish.yml`, `release.yml`,
`security-audit.yml`) are tag-pinned by the family convention; that is not
covered here.

**Local proof:** `make image-smoke` (the image from this checkout's wheel, every
engine), `make image-scan`, `make image-resolve` (the `resolve` job for real under
act: both registries, GHCR alone, and the configurations that must fail), `make ci-dryrun` (every job's
refs, the Docker Hub path included). Multi-arch builds, pushes and attestations
are GitHub-only.
