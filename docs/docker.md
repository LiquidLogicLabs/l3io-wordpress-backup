# The official Docker image

`wpbackup2` as a sidecar next to the official `wordpress` image (owner decisions
77–80). The README's [Run as a Docker sidecar](../README.md#run-as-a-docker-sidecar)
has the compose example; this page is the reference.

```
ghcr.io/liquidlogiclabs/l3io-wordpress-backup:<tag>
docker.io/ravensorb/l3io-wordpress-backup:<tag>
```

Both public, the same digest in each. Two variants, by dump client (owner
decision 81): the default (`1`, `latest`), with MySQL's client, and the
MariaDB-client one (`1-mariadb`, `mariadb`). [Which variant](#which-variant)
says which to pick.

## What is in it

| Part | Version | How it is pinned |
|---|---|---|
| Base | `ghcr.io/linuxserver/baseimage-ubuntu:resolute` (Ubuntu 26.04, s6-overlay) | by digest (`BASE_IMAGE` build-arg) |
| `l3io-wordpress-backup` | the PyPI release named by `VERSION` | `==VERSION`, asserted after the install |
| Python | Ubuntu 26.04's `python3` (3.14) | the base |
| Dump/load client | Ubuntu's `mysql-client` 8.4; the `-mariadb` variant: `mariadb-client` 11.8 | the base's archive (`DB_CLIENT` build-arg) |
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
restarts Supercronic if it exits (started with `--user` there is no s6: the
container exits with Supercronic, for its restart policy), and a failed job is
in the container log with its exit status, which a container health state would
not say more clearly.

## Which variant

**MariaDB servers: the `-mariadb` variant** (`:1-mariadb`). **MySQL servers:
the default** (`:1`). One build-arg, `DB_CLIENT`, is the only difference;
Ubuntu's `mysql-client` and `mariadb-client` conflict, so an image carries one.

The default image's MySQL 8.4 `mysqldump` dumps a MariaDB server too, but logs
WARNING `client_server_skew` (its `event` and `code`) on every dump (a
client of the other family). The `-mariadb` variant's `mariadb-dump` does not.
Its dump fails against MySQL, so it is never the image for a MySQL server.
Proven with the full sidecar suite (`tests/sidecar/run.sh`, every asserted
step and check, which also asserts the skew warning appears exactly when the
client's family is not the server's):

| Server | default (MySQL 8.4 client) | `-mariadb` (MariaDB 11.8 client) |
|---|---|---|
| `mysql:8.0` | passes | **fails**: the first backup's dump exits 2 (`client_server_skew`) |
| `mysql:8.4` | passes | **fails**: the first backup's dump exits 2 (`client_server_skew`), measured 2026-11-11 |
| `mariadb:11.4` | passes, with `client_server_skew` (`mysqldump` with `--column-statistics=0`, l3io-wordpress-database 0.3.5) | passes, no warning |

The release pipeline runs each variant's suite on the servers it is for: the
default on all three, the `-mariadb` variant on MariaDB
(`tests/contracts/test_docker_image.py` holds the two in step with the
Dockerfile's `DB_CLIENT` choices).

## Two modes

**Scheduled (the default command).** Started as root (the default), the
base's s6 init starts; `init-wpbackup-config` checks the folders the jobs need
and builds the crontab, and `svc-wpbackup-schedule` runs Supercronic, both as
`PUID:PGID` (`l3io-wpbackup-scheduler`). Started as another user (`--user`,
compose `user:`), there is no s6 (it needs root): the entrypoint makes the same
check, builds the same crontab and runs Supercronic as that user directly
([Users](#users)). Every job's output goes to the container log unchanged
(`-passthrough-logs`). A folder the jobs' user cannot use, or a missing or
unparsable schedule, stops the container at start
(`S6_BEHAVIOUR_IF_STAGE2_FAILS=2`; exit 3 or 2 without s6) rather than failing
every night ([Ownership](#ownership)).

| Variable | Default | Meaning |
|---|---|---|
| `WPBACKUP_SCHEDULE` | `0 3 * * *` | one cron expression for `wpbackup2 backup`; empty to schedule only the file below |
| `WPBACKUP_KEEP_DAILY` | `7` | after each **successful** scheduled backup, `wpbackup2 prune --keep-daily N`: the newest Archive of each of the last N UTC days that have one ([how retention works](#how-retention-works); owner decision 84) |
| `WPBACKUP_KEEP_WEEKLY` | `4` | `--keep-weekly N`: the newest of each of the last N ISO weeks (Monday to Sunday, UTC) that have one |
| `WPBACKUP_KEEP_MONTHLY` | `6` | `--keep-monthly N`: the newest of each of the last N months that have one |
| `WPBACKUP_KEEP_YEARLY` | unset | `--keep-yearly N`: the newest of each of the last N years that have one |
| `WPBACKUP_KEEP` | unset | `--keep N`: this site's newest N Archives (owner decision 83) |
| `WPBACKUP_CRONTAB` | `/etc/l3io-wp-backup/crontab` | a crontab appended as written, when it exists (named explicitly, it must exist) |
| `L3IO_WP_BACKUP_ARCHIVE_SUBDIR` | unset (flat) | `wpbackup2`'s own `--archive-subdir` setting, read by every `backup`, scheduled or one-shot: the subfolder of `/backups` the Archive goes in, e.g. `{site}/{yyyy}` ([Archive subfolders](#archive-subfolders); owner decision 86) |
| `L3IO_WP_BACKUP_COMPRESSION_LEVEL` | unset (6) | `wpbackup2`'s own `--compression-level` setting, passed through as is: the gzip level of each Archive's payload, and of a restore's Safety Snapshot, from 1 (fastest) to 9 (smallest); outside 1–9 every `backup` and `restore` exits 2 (owner decision 88) |
| `TZ` | UTC | the schedule's time zone |

Each `WPBACKUP_KEEP*` rule is passed to the prune only when it is set and not
`0`; the prune keeps what any of them keeps. **All of them unset, empty or `0`
disables pruning** (set `WPBACKUP_KEEP_DAILY=0 WPBACKUP_KEEP_WEEKLY=0
WPBACKUP_KEEP_MONTHLY=0` to turn the defaults off). A value that is not a whole
number (at most 9 digits) stops the container at start. To keep only the
newest N instead: `WPBACKUP_KEEP=14` and the three tiers `0`.

The image's own variables are `WPBACKUP_*`, not `L3IO_WP_BACKUP_*`: `wpbackup2`
reads every `L3IO_WP_BACKUP_*` variable as a setting and logs WARNING
`unknown_setting_variable` on every Run for one it does not know.
`tests/contracts/test_docker_image.py` checks that every `L3IO_WP_BACKUP_*`
name the image sets or reads is a `wpbackup2` setting, and that the scheduler
maps every retention rule.

Supercronic does not start a job again while its previous run is still going,
and the Site Lock refuses a second Run on the same site anyway. Avoid an
impossible date (`0 0 31 2 *`): Supercronic 0.2.51 accepts it and then logs a
warning in a busy loop.

The prune follows the `WPBACKUP_SCHEDULE` job only (by default `0 3 * * *
wpbackup2 backup && wpbackup2 prune --keep-daily 7 --keep-weekly 4
--keep-monthly 6`), so a failed backup deletes nothing; jobs in a mounted
crontab are run as written. It deletes only Archives whose Manifest is this
site's: another site's Archives in the same volume, Safety Snapshots and files
that are no Archive are never touched.

### Archive subfolders

`/backups` is flat by default. To sort a site's Archives by year, as the
estate does:

```yaml
    environment:
      L3IO_WP_BACKUP_SITE_NAME: shop
      L3IO_WP_BACKUP_ARCHIVE_SUBDIR: "{site}/{yyyy}"
```

Each backup then writes `/backups/shop/2026/wpbak-shop-20261112T030000Z.tar.gz`.
The template is a relative path using `{site}` (the Archive name's `<site>`:
`L3IO_WP_BACKUP_SITE_NAME`, else the database name), `{yyyy}`, `{mm}` and
`{dd}` (the UTC date of the name's timestamp), at most 6 levels; a bad one
stops each Run with exit 2, and its reason, before anything is read. One
one-shot Run can still write flat: `wpbackup2 backup --archive-subdir none`
(`none` is the setting's word for no subfolder). The folders are made
`0700`, owned by the user the backup runs as, like the Archives. It is `wpbackup2`'s own setting, not a `WPBACKUP_*` one:
the scheduled job and a one-shot `backup` both read it. `restore --archive
latest` and the scheduled prune search every subfolder (never through a
symlinked folder), matching by Manifest, so flat Archives from before the
change are still found and pruned, and the prune removes the folders it
empties (`prune --whatIf` lists them: `would remove empty folder ...`). `tests/sidecar/run.sh` runs the schedule with
`{site}/{yyyy}/{mm}`.

### How retention works

The grandfather-father-son model, as restic's `forget` applies it (owner
decision 84; [ADR-0029](adr/0029-tiered-retention-bucketing.md)). Each rule
looks at this site's Archives newest first, by the `created_at` their Manifest
records (UTC), and keeps:

- `--keep-daily N`: the newest Archive of each of the last N **UTC days** that
  have one;
- `--keep-weekly N`: the newest of each of the last N **ISO weeks** (Monday to
  Sunday; Thu 31 Dec 2026 and Fri 1 Jan 2027 are one week, 2026-W53);
- `--keep-monthly N` and `--keep-yearly N`: the same per calendar month and
  year;
- `--keep N`: the newest N.

A day, week, month or year with no Archive does not count, so a gap reaches
further back instead of losing coverage. An Archive kept by **any** rule
stays; the newest always does. Everything else of this site's is deleted.
Nothing is copied or renamed: one nightly Archive can be the daily, the weekly
and the monthly at once, and each rule keeps it for as long as that rule
reaches.

**Promotion, with the defaults (7 daily, 4 weekly, 6 monthly) and nightly
backups.** The backup of Sunday 31 August 2025:

| On | Kept by | Why |
|---|---|---|
| 31 Aug – 6 Sep | daily, weekly, monthly | one of the last 7 days; the newest of its week (2025-W35) and of August |
| 7 – 21 Sep | weekly, monthly | 7 newer days now; W35 is still one of the last 4 weeks |
| 22 Sep – 31 Jan 2026 | monthly | W39 starts: W35 is the fifth week; August is still one of the last 6 months |
| 1 Feb 2026 | — (deleted) | February is the seventh month with an Archive |

Monday 1 September's backup is never the newest of its week or month by the
time the daily window passes it, so it is deleted on 8 September. With
nightly backups the defaults hold 13 to 15 Archives at any time: the 7 newest
days, the Sundays of the weeks before them, and the last days of the months
before those (fewer when one Archive is kept by two rules).

`wpbackup2 --whatIf prune ...` lists every Archive it would keep with the
rules that keep it, and every one it would delete with why; each Run logs
`archive_pruned` with `reasons` (and `archive_kept` at DEBUG).

**One-shot (any other command).** `docker run IMAGE backup` is `wpbackup2
backup`; `docker run IMAGE wpbackup2 backup` is the same. A one-shot run never
prunes unless asked: `docker run IMAGE prune --keep-daily 7 --keep-weekly 4
--keep-monthly 6` (the `WPBACKUP_KEEP_*` variables feed the schedule only). The first argument
runs as a command only when it names an executable on PATH (`sh`, `sleep`);
`tests/sidecar/run.sh` checks that no `wpbackup2` subcommand does. No s6 starts.

## Users

The image runs as the user it is given, in both modes, and never runs
`wpbackup2` as root unless told to (owner decision 87):

| Started as | Runs as |
|---|---|
| root (default), `PUID`/`PGID` unset | 33:33, www-data in the official `wordpress` image |
| root, `PUID=1000 PGID=1000` | 1000:1000 (`setpriv`; s6 for the scheduled mode) |
| root, `PUID=0` | root; `wpbackup2` then runs each backup, restore and prune as the Site Root's owner itself (owner decisions 58, 67), and the scheduled mode's start check is made as that owner |
| `--user 33:33` / `user: "33:33"` | 33:33, in both modes (the scheduled mode without s6); a read-only root filesystem works ([Writable paths](#writable-paths)) |
| `--user 33:33` with `PUID`/`PGID` naming another user | refused at start, exit 2: a container not started as root cannot switch users. Unset them, or set them to the same ids |

`PUID`/`PGID` are not set in the image's environment, so a `--user` container
can tell that no other user was asked for. `docker compose exec backup wpbackup2
...` into a root-started scheduled container runs as root (the container's
user) unless `-u 33` is given; `wpbackup2` then reads its credentials as root
and switches to the Site Root's owner, as above. Started with `--user`, add
`init: true` (compose) or `--init` so a process reaps the jobs' orphans:
Supercronic is otherwise the container's first process.

## Ownership

**The image never changes the owner, group or mode of anything you mount**
(owner decision 87): not the Archives, the state (Safety Snapshots, the Run
Journal, the Site Lock), `TMPDIR` or the site's files. Owning them is the
operator's job. wpbackup2's own new files and folders are its user's, as any
program's are (Archives `0600`, folders `0700`); a restore writes the Site's
files as the Site Root's owner.

**What fails, and how.** A folder the user cannot use is refused before
anything is done, naming the path, the user's `uid:gid`, the owner, group and
mode found, and the fix:

- **Scheduled mode, at start:** the state directory, `TMPDIR`, the Archive
  directory (write) and the Site Root and its `wp-config.php` (read) are
  checked as the jobs' user; every one it cannot use is listed and the
  container stops. A Site Root that is not there yet is left to each backup.
  A job of a mounted crontab that names other folders by flag is checked by
  `wpbackup2` when it runs.
- **Every Run:** `wpbackup2` checks, before the Run opens, a Dry Run too, the
  state directory (write; `backup`, `restore`, `prune`), `TMPDIR` (write;
  `backup`, `restore`), the Archive directory (write; `backup`, `prune`), the
  Archive (read; `restore`, `verify`), a backup's Site Root (read) and a
  restore's `--snapshot-dir` (write): exit 3, `PathNotAccessibleError`. A
  restore's Site Root is checked by its target survey (exit 3,
  `TargetNotWritableError`).

```
wpbackup2: error: the state directory '/var/lib/wpbackup/state/l3io-wp-backup' is not writable by uid 12345:12345 (checked at '/var/lib/wpbackup/state', owned by 33:33, mode 0700); wpbackup2 never changes ownership: give 12345:12345 write access to it (for example chown -R 12345:12345 on the host folder or volume mounted there), or run wpbackup2 as 33:33 (in the Docker image, PUID=33 PGID=33, or --user 33:33); nothing was changed [exit 3 REFUSED: ...]
```

**The fix is yours:** run as the folders' owner (`PUID`/`PGID`, or `--user`),
or give the folders to the user you run as. A named volume's folder, from the
host: `docker run --rm -v <volume>:/v alpine chown -R 33:33 /v`; a bind mount:
`sudo chown -R 33:33 /srv/backups/<site>`. A new named volume mounted at
`/backups` or `/var/lib/wpbackup/state` starts as the image's folder is
built, `33:33 0700`, so the default user needs nothing.

**One state per user.** The state directory holds the Site Lock and the Run
Journal, and wpbackup2 keeps it only in a folder the user it runs as owns. Run
every backup, restore and prune of a site as the same user (with `PUID=0`, the
Site Root's owner), so they share one lock; a one-shot as another `PUID`
against the same state volume is refused, and changes nothing.

**What the base image does.** linuxserver's s6 init runs only when the
container starts as root in the scheduled mode. `init-adduser` re-numbers its
`abc` user to `PUID:PGID` in `/etc/passwd` (this image does not use `abc`) and
re-owns the top of `/app`, `/config` and `/defaults` (not recursively);
`init-crontab-config` re-owns `/etc/crontabs/abc|root` and `/config/crontabs`
when they exist; `init-device-perms` changes devices named by
`ATTACHED_DEVICES_PERMS` only. This image uses none of those paths and sets
neither variable, so **do not mount anything at `/app`, `/config` or
`/defaults`**. None of it touches `/backups`, `/var/lib/wpbackup/state`,
`/tmp/wpbackup` or `/var/www/html`. `tests/contracts/test_docker_image.py`
fails if any script this image adds (everything under `docker/root`) changes an
owner, group or a tree's mode; the base's scripts are outside that check, and
`tests/sidecar/run.sh` asserts the shared volumes keep every owner and mode
across a run as another `PUID`.

### Restoring an Archive another user wrote

The usual disaster-recovery case: the Archive in `/backups` was written by one
uid (the old host's), and the Site Root (`/var/www/html`, the new
`wordpress` container's volume) belongs to another (33 in the official
image). Run the restore with `PUID=0`: `wpbackup2` starts as root, reads the
Archive and the passphrase, then switches to the uid and gid that own the
Site Root for the rest of the Run (owner decisions 58 and 67), so the restored
files are the site's:

```
docker compose run --rm -e PUID=0 backup restore --archive latest
docker compose exec -u 0 backup wpbackup2 restore --archive latest   # into the running sidecar
```

The restored files are the Site Root owner's because the Run is that user
(the switch changes who writes, not who owns anything else); the state
directory must be that user's too (the same rule as every Run,
[Ownership](#ownership)): a Site Root owned by 1000 needs a state volume owned
by 1000. A Site Root owned by root has no owner to switch to and is refused
(exit 2: "refused to restore as root", "back up" or "prune", each in its own
words); `chown -R 33:33` it first.

Run instead as a uid that is not the Site Root's owner (the default 33
against a Site Root owned by 1000, say), the restore is refused before it
changes anything, a Dry Run too. Which refusal depends on what that uid can
read: **exit 4** (`CREDENTIALS`) when it cannot read the site's
`wp-config.php` (a `0640` file of 1000's, as WordPress hosts commonly set it),
which is read first; **exit 3** (`TargetNotWritableError`, naming the paths
and the uid) only when it can read the config and the Archive but cannot write
the Site Root (a `0644` config in a `0755` Site Root, say). Either way nothing
was changed; `PUID=0` is the fix.

## Writable paths

| Path | What | Mount |
|---|---|---|
| `/backups` (`L3IO_WP_BACKUP_ARCHIVE_DIR`) | the Archives | a volume |
| `/var/lib/wpbackup/state` (`XDG_STATE_HOME`) | the Site Lock, the Run Journal, default Safety Snapshots | a volume (it must survive the container for an interrupted Run to be resolved) |
| `/tmp/wpbackup` (`TMPDIR`) | the Run's workspace, holding the plaintext Dump during a Run, and the scheduled mode's crontab (`wpbackup-schedule-<uid>/`, `0700`) | a tmpfs: `mode=1777` (any user), or `mode=0700,uid=33,gid=33` for one user |
| `/var/www/html` (`L3IO_WP_BACKUP_WP_DIR`) | the Site | the `wordpress` container's volume |

The image's own folders need no chown for any user: `/tmp/wpbackup` is built
sticky and world-writable like `/tmp` (each Run's workspace and the schedule's
folder under it are private, `0700`), and `HOME` (`/home/wpbackup`) is
root's and read-only, since nothing the image runs writes to it. The two data
folders are built `33:33 0700`, the default user's ([Ownership](#ownership)).

**Read-only root filesystem** (`read_only: true`): started with `--user`, in
either mode, it needs only the mounts above and a tmpfs at `/tmp/wpbackup`
(`tests/sidecar/compose.yaml`'s `backup` service, one-shot, and
`scheduler-user`, scheduled). Started as root, the scheduled mode needs a
writable root: the base's init rewrites `/etc/passwd` and s6 writes under
`/run`. Use `--user` for a read-only scheduled container.

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

| | default | MariaDB client |
|---|---|---|
| every build | `X.Y.Z` | `X.Y.Z-mariadb` |
| PyPI's latest release only | `X.Y`, `X`, `latest` | `X.Y-mariadb`, `X-mariadb`, `mariadb` |

The floating tags move only when `X.Y.Z` is PyPI's latest release, so
rebuilding an older release never moves them backwards. One build per variant,
one digest each, pushed to every enabled registry.

## Release pipeline

[.github/workflows/docker.yml](../.github/workflows/docker.yml), after
`publish.yml` has published to PyPI (or dispatched with `version`):

1. **resolve**: the Publish run's `publish to PyPI` job succeeded (else nothing
   is built); the version installs from PyPI (waits up to 30 min); the
   registries (below). It references no secret: its outputs are the image
   names, and GitHub drops a job output that contains a referenced secret's
   value.
2. **credentials**: each enabled registry's login is present (below), before
   anything is built.
3. **build**, once per variant: each platform built and scanned (Trivy,
   below), then both pushed **by digest only**, with an SBOM and `mode=max`
   provenance.
4. **smoke**: `tests/sidecar/run.sh` against each variant's digest, pulled from
   the registry: the default on `mysql:8.0`, `mysql:8.4` and `mariadb:11.4`, the
   `-mariadb` variant on `mariadb:11.4`.
5. **publish**, once per variant: its tags applied to its tested digest in every
   registry; a GitHub build-provenance attestation (Sigstore) pushed beside it;
   then every published tag must pull anonymously (the image is public, owner
   decision 80).

**Registries**, configured as the family's image releases are
(`LiquidLogicLabs/markdown-mermaidjs-to-pdf`'s `release.yml`), by repository
variables with these defaults:

| Variable | Default | Meaning |
|---|---|---|
| `REGISTRY_NAME`, `REGISTRY_ORG`, `IMAGE_NAME` | `ghcr.io`, `liquidlogiclabs`, `l3io-wordpress-backup` | the primary registry, always pushed; logs in with the secrets `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` when set, else `GITHUB_TOKEN` |
| `DOCKERHUB_ENABLED` | `true` | also push `docker.io/<DOCKERHUB_ORG>/<IMAGE_NAME>`, logging in as the variable `DOCKERHUB_USERNAME` with the secret `DOCKERHUB_TOKEN`; enabled without both, `credentials` fails before building |
| `DOCKERHUB_USERNAME` | none | the Docker Hub login. A variable, never a secret: it equals the org, and as a secret it masks the image name, which drops resolve's `names` output |
| `DOCKERHUB_ORG` | `ravensorb` | the Docker Hub namespace (the `liquidlogiclabs` namespace refused the push) |
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

**Local proof:** `make image-smoke` (each variant from this checkout's wheel, on
the engines docker.yml smokes it on: `IMAGE_LEGS`), `make image-scan` (`IMAGE=`
to scan the other variant), `make image-resolve` (the `resolve` and `credentials` jobs for real
under act: both registries, GHCR alone, and the configurations that must fail), `make ci-dryrun` (every job's
refs, the Docker Hub path included). Multi-arch builds, pushes and attestations
are GitHub-only.
