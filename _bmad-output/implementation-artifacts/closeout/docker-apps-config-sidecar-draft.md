# Draft for Shawn's approval: an l3io-wordpress-backup sidecar per WordPress site

**Status:** draft, 2026-11-12. Nothing changes until Shawn approves. Then the docker-apps-config agent edits the repo and deploys one site at a time.

**Evidence:** these all passed with `1.1.0-mariadb` on rwusdckrls6 (itramblings, eyecatcher):
- one-shot backup and verify;
- the scheduled mode;
- a prune Dry Run that recognises old and new Archive names;
- restores on the test host that match a direct load of the dump table for table;
- no client warning and no password in any log.

The old sidecar has made no backups since 2026-10-24: its `@daily` job in `wordpress-<site>-tools` fails with exit 127.

## Per site: a new container in `src/linux/wordpress/<site>/app.json`

There's no shared WordPress template; every site carries its own containers. Field names follow the existing container entries.

```json
{
  "service": "wordpress-{{appname}}-backup",
  "imageName": "ravensorb/l3io-wordpress-backup",
  "tag": "1.1.0-mariadb",
  "hubUrl": "https://hub.docker.com/r/ravensorb/l3io-wordpress-backup",
  "watchtower": false,
  "environment": [
    { "name": "PUID", "value": "33" },
    { "name": "PGID", "value": "33" },
    { "name": "L3IO_WP_BACKUP_SITE_NAME", "value": "{{appname}}" },
    { "name": "WPBACKUP_SCHEDULE", "value": "{{wordpress-{{appname}}-backup-schedule}}" },
    { "name": "TZ", "value": "UTC" }
  ],
  "volumes": [
    { "source": "{{path-wordpress-wwwroot}}", "target": "/var/www/html:ro" },
    { "source": "{{path-wordpress-backups-websites}}/{{appname}}", "target": "/backups:rw" }
  ],
  "networks": [ { "name": "data", "mode": "external" } ]
}
```

| Item | Value | Why |
| --- | --- | --- |
| Image | `ravensorb/l3io-wordpress-backup:1.1.0-mariadb`, or the same on `ghcr.io/liquidlogiclabs/…` | Every site's database is MariaDB 11.4.12; this variant uses MariaDB's own `mariadb-dump`. Pinning the exact version is a deliberate upgrade step. A digest pin works if the family rule calls for one. |
| Database settings | none | The image reads the site's `wp-config.php`, which holds literal values on these sites. No DB env vars or secrets are needed. |
| Webroot | read-only at `/var/www/html` | Backups only read it. A restore is run separately, as a one-shot. |
| Backups | `/mnt/backups-websites-wordpress/<site>` → `/backups` | The new RWUSFS3 share mount, uid/gid 33, 0750. `/mnt/backups-general` is forced to uid 30102, so 33 can't write there. This needs a new path variable, e.g. `path-wordpress-backups-websites = /mnt/backups-websites-wordpress`. |
| Network | `data` | It reaches `wordpress-<site>-db` there. |
| Retention | image defaults: 7 daily, 4 weekly, 6 monthly | Shawn, 2026-11-12. That's about 13–15 Archives per site; nothing to set. |
| Archive name | `wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz` | From `L3IO_WP_BACKUP_SITE_NAME`. |
| Subfolders | none (flat per-site folder) | Optional later: `L3IO_WP_BACKUP_ARCHIVE_SUBDIR={yyyy}`. |
| Schedule | a per-site staggered time, UTC | So the sites don't all dump and gzip at once on one host and one share. Suggested times below. |
| Mode | default (scheduled; s6 and Supercronic) | Runs as root only to drop to PUID 33. Not `read_only`: the scheduled mode needs a writable root. |
| Logs | container stdout (JSON lines) | One `run_finished` per backup, and one `prune_completed` with what was kept and deleted. |

**Recommended, if the generator supports it** (to confirm with the docker-apps-config agent):
- `tmpfs: /tmp/wpbackup` (size about 3 GB for itramblings), so the plaintext dump never touches disk;
- a memory limit of 512M–1G;
- `restart: unless-stopped`.

## Per site: the old job in `wordpress-<site>-tools`

Set `CRON_ENABLED=false`, or remove the `@daily /opt/setup/app/wordpress-backup-daily.sh` entry. It fails every night, and the new sidecar replaces it. Keep the tools container itself, since WP-CLI lives there.

## Suggested schedule (UTC, 10 minutes apart, largest site last)

`eyecatcher 03:00`, `itramblings 03:10`. When it rolls out to the other sites, each gets the next 10-minute slot. The schedule is a per-site variable, so moving a site is one value.

## Rollout, under the estate rule (one site first, verify, then the rest one at a time)

1. **eyecatcher:** add the container, deploy that site only, wait for the first scheduled run (or trigger a one-shot with `docker exec … wpbackup2 backup`), then check `run_finished` exit 0 and that the Archive appears.
2. **itramblings:** the same.
3. The remaining sites one at a time, each with its own `/mnt/backups-websites-wordpress/<site>` folder created 33:33 first. Check every database is MariaDB; a MySQL site would use the default image tag instead.
4. Later: let Dependabot or a manual bump move the tag to new releases.

## Restore (documented in docs/docker.md)

A restore is a one-shot run, not part of the sidecar. Run `docker run --rm` with the webroot mounted read-write, the Archive mounted, `PUID=0` (the CLI switches to the site owner) and `restore --archive latest`. It takes a Safety Snapshot first and rolls back automatically on failure.

## Questions for Shawn

1. Approve the container block and the `-tools` cron change?
2. Stagger the schedule as above, or one time for all sites?
3. Docker Hub (`ravensorb/…`) or GHCR (`ghcr.io/liquidlogiclabs/…`) as the source? They are the same image.
