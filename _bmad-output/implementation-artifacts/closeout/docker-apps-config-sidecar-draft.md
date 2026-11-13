# Draft for Shawn's approval: an l3io-wordpress-backup sidecar per WordPress site

**Status:** draft v2, 2026-11-13. It reflects Shawn's answers on the schedule (staggered), the registry (GHCR) and the start modes (support both). **Shawn's decision on `-tools`:** keep a tools container per site, but switch it to the official `wordpress:cli` image (see the `-tools` section). Nothing changes until Shawn approves. Then docker-apps-config - agent 1 edits the repo and deploys one site at a time.

**Evidence**
- **On rwusdckrls6, itramblings and eyecatcher** (`1.1.0-mariadb`): one-shot backup and verify, the scheduled mode, a prune Dry Run that recognises old and new Archive names, and no client warning and no password in any log.
- **Restore checks on rwusdevl2:** matched a direct load of the dump table for table, the site served 200, and restore over an existing site worked.
- **1.1.1** adds no runtime ownership changes (decision 87) and gzip level 6 (decision 88). Its sidecar test passes on both variants, including `user: 33:33` with a read-only root.
- **The old sidecar** has made no backups since 2026-10-24: its `@daily` job in `wordpress-<site>-tools` fails with exit 127.

## Per site: a new container in `src/linux/wordpress/<site>/app.json`

There's no shared WordPress template; every site carries its own containers. Field names follow the existing container entries. `user`, `init`, `read_only` and `tmpfs` need the generator to support them; the docker-apps-config agent must confirm, and if one isn't supported, use the root-plus-PUID alternative below.

**Default: started as uid 33 (no root, no s6)**

```json
{
  "service": "wordpress-{{appname}}-backup",
  "imageName": "ghcr.io/liquidlogiclabs/l3io-wordpress-backup",
  "tag": "1.1.1-mariadb",
  "hubUrl": "https://github.com/LiquidLogicLabs/l3io-wordpress-backup/pkgs/container/l3io-wordpress-backup",
  "watchtower": false,
  "user": "33:33",
  "init": true,
  "read_only": true,
  "tmpfs": [ "/tmp/wpbackup:size=3g,mode=1777" ],
  "environment": [
    { "name": "L3IO_WP_BACKUP_SITE_NAME", "value": "{{appname}}" },
    { "name": "WPBACKUP_SCHEDULE", "value": "{{wordpress-{{appname}}-backup-schedule}}" }
  ],
  "volumes": [
    { "source": "{{path-wordpress-wwwroot}}", "target": "/var/www/html:ro" },
    { "source": "{{path-wordpress-backups-websites}}/{{appname}}", "target": "/backups:rw" },
    { "source": "{{path-app-base-data}}/backup-state", "target": "/var/lib/wpbackup/state:rw" }
  ],
  "networks": [ { "name": "data", "mode": "external" } ]
}
```

**Alternative: started as root, dropping to PUID/PGID** (s6; needs a writable root). Same block, without `user`, `init`, `read_only` and `tmpfs`, and with these added to the environment:
`{ "name": "PUID", "value": "33" }, { "name": "PGID", "value": "33" }`.

| Item | Value | Why |
| --- | --- | --- |
| Image | `ghcr.io/liquidlogiclabs/l3io-wordpress-backup:1.1.1-mariadb` (or `1.1.2-mariadb` once released) | Every site runs MariaDB 11.4.12; this variant uses MariaDB's `mariadb-dump`, with no skew warning. Pinning the exact version makes upgrades deliberate. A digest pin works too, if the family rule calls for one. |
| User | `33:33` (www-data) | Owns the webroot and the backups share. **The image never changes ownership** (decision 87). Every mounted folder must already be readable or writable by 33, or the container refuses to start and names the folder and the fix. |
| Database settings | none | The image reads each site's own `wp-config.php` (literal values on these sites). No DB env vars or secrets. |
| Webroot | read-only at `/var/www/html` | A backup only reads it. A restore is run separately, as a one-shot. |
| Backups | `/mnt/backups-websites-wordpress/<site>` → `/backups` | The RWUSFS3 share mount (uid/gid 33, 0750). `/mnt/backups-general` is forced to uid 30102, so 33 can't write there. **New path variable:** `path-wordpress-backups-websites = /mnt/backups-websites-wordpress`. Each site's folder must be created 33:33 first. |
| State (locks, journal, Safety Snapshots) | a per-site folder owned 33:33 | Kept across restarts. It must not be shared with a different uid. |
| Network | `data` | It reaches `wordpress-<site>-db` there. |
| Schedule | staggered, UTC, 10 minutes apart (below) | Shawn chose staggering. |
| Retention | image defaults: 7 daily, 4 weekly, 6 monthly | Shawn chose these. About 13–15 Archives per site; nothing to set. |
| Archive name | `wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz` | From `L3IO_WP_BACKUP_SITE_NAME`. |
| Compression | gzip level 6 (default) | Override with `L3IO_WP_BACKUP_COMPRESSION_LEVEL`. |
| Subfolders | none (one flat folder per site) | Optional later: `L3IO_WP_BACKUP_ARCHIVE_SUBDIR={yyyy}`. |
| Logs | container stdout (JSON lines) | One `run_finished` per backup, and one `prune_completed` with what was kept and deleted. |

Also recommended: a memory limit of 512M–1G and `restart: unless-stopped`.

## Schedule (UTC, 10 minutes apart)

`eyecatcher 03:00` (`0 3 * * *`), `itramblings 03:10` (`10 3 * * *`). Later sites take the next 10-minute slots. The schedule is the per-site variable `wordpress-<site>-backup-schedule`.

## `-tools`: switch to the official `wordpress:cli` image (Shawn, 2026-11-13)

`wordpress-<site>-tools` keeps its one remaining job, WP-CLI for the estate, on the official image. The custom `ravensorb/wordpress-tools` image and its broken `@daily` backup job are retired; the backup sidecar above replaces that job.

```json
{
  "service": "wordpress-{{appname}}-tools",
  "imageName": "wordpress",
  "tag": "cli-2.12-php8.4",
  "hubUrl": "https://hub.docker.com/_/wordpress",
  "user": "33:33",
  "init": true,
  "command": [ "sleep", "infinity" ],
  "volumes": [
    { "source": "{{path-wordpress-wwwroot}}", "target": "/var/www/html:rw" },
    { "source": "{{path-wordpress-backups}}/{{appname}}", "target": "/backups:rw" }
  ],
  "networks": [ { "name": "data", "mode": "external" } ]
}
```

| Item | Value | Why |
| --- | --- | --- |
| Tag | `cli-<wp-cli>-php<ver>`, matched to each site's web image PHP (itramblings `7.1.3-php8.4-apache` → `cli-2.12-php8.4`) | WP-CLI loads the site's own PHP; the versions must agree. |
| User | `33:33` | The official image's `www-data` is **uid 82** (Alpine). Running as 82, or as root, would leave files the site (uid 33) can't use. As 33, WP-CLI writes files the site owns, so no more after-the-fact `chown`. |
| Command | `sleep infinity` with `init: true` | The image's default runs `wp` once and exits. This keeps the container up for `docker exec … wp …`. |
| Webroot | `/var/www/html` (read-write) | That's the official image's working directory. **Changed from `/data`.** |
| Backups mount | the existing `{{path-wordpress-backups}}/{{appname}}` → `/backups` | For manual `wp db export /backups/pre-<change>.sql` snapshots. It must be writable by 33. `/mnt/backups-general` is forced to uid 30102, so either use the new websites share or keep pre-change exports in `/tmp` and copy them out. **The docker-apps-config agent must check this.** |
| DB settings and env | none | WP-CLI reads `wp-config.php`. The old `WORDPRESS_DB_*`, `WORDPRESS_BACKUP_*` and `CRON_ENABLED` variables go away with the custom image. |

**Follow-on change outside docker-apps-config:** the home-lab plugin's `wordpress-estate` skill must change its WP-CLI instructions:
- from `docker exec wordpress-<site>-tools wp --allow-root --path=/data …`;
- to `docker exec wordpress-<site>-tools wp --path=/var/www/html …` (no `--allow-root`, since it runs as 33);
- and drop the "WP-CLI writes are root-owned; chown afterwards" rule.

## Rollout, under the estate rule (one site first, verify, then the rest one at a time)

1. **Prepare:** create `/mnt/backups-websites-wordpress/<site>` and the state folder, owned 33:33.
2. **eyecatcher:** add the container, deploy that site only, and wait for the 03:00 UTC run (or trigger a one-shot). Check `run_finished` exit 0 and that the Archive appears. Then switch that site's `-tools` to `wordpress:cli` and check `docker exec wordpress-<site>-tools wp core version` as 33.
3. **itramblings:** the same, at 03:10.
4. **Remaining sites, one at a time:** check each database is MariaDB first; a MySQL site would use the default tag.
5. **Later:** move the tag to new releases deliberately.

## Restore (documented in docs/docker.md)

A restore is a one-shot run, not part of the sidecar: `docker run --rm` with the webroot mounted read-write, the Archive mounted, then `restore --archive latest`, run as `--user 33:33`, or with `PUID=0` when the Archive is owned by someone else. It takes a Safety Snapshot and rolls back automatically on failure.

## As deployed: eyecatcher, 2026-11-13 (docker-apps-config-servers 0641b16, hand-placed on rwusdckrls6)

- **Generator support:** `user` and `command` are supported. `tmpfs` renders but isn't in the schema. `init` and `read_only` are not supported.
- **Backup container:** the root-plus-PUID alternative, `PUID=33`, `PGID=33`. It sets `TZ=Etc/UTC`, because the repo defaults inject `TZ=America/New_York` and PUID/PGID 1000, and Supercronic uses the container's time zone.
- **Tools container:** `user: 33:33`, kept up by `sh -c "trap 'exit 0' TERM INT; sleep infinity & wait"` (no `init`). Its `/backups` is `/mnt/backups-websites-wordpress/<site>-manual` (33:33), a sibling of the Archive folder, so `prune` never sees manual exports. `/mnt/backups-general` is forced to uid 30102 and refused a write as 33.
- **New path variable:** `path-wordpress-backups-websites=/mnt/backups-websites-wordpress`, in the tracked `src/linux/dockerApps.json`.
- **Verified:** tools runs `wp --path=/var/www/html core version` (7.1.4, uid 33, no `--allow-root`). The backup's crontab is `0 3 * * * wpbackup2 backup && wpbackup2 prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6`. A one-shot backup gave exit 0 in 28 s, producing `wpbak-eyecatcher-20261113T220241Z.tar.gz`. The site returns 200.
- **Next:** itramblings (`10 3 * * *`), when Shawn approves. The wordpress-estate skill change is with home-lab - agent 6.
