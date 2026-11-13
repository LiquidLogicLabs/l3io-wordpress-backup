# Plan: move every WordPress site on rwusdckrls6 to the new backup process

**Status:** PLAN ONLY, 2026-11-13. Nothing is executed until Shawn approves.
**Source:** the read-only inventory by docker-apps-config - agent 1 (2026-11-13 ~22:30Z), plus the eyecatcher and itramblings rollouts (docker-apps-config-servers 0641b16 and bd0d0e6, both local).

## The prime rule: no WordPress data is affected

- **Never restarted or recreated:** the web (`wordpress-<site>`) and database (`wordpress-<site>-db`) containers. Every compose action is `up -d --no-deps <site>-tools <site>-backup` only.
- **The backup container is read-only towards the site.** It mounts the webroot read-only and dumps with `--single-transaction` (no locking writes). It never changes ownership of anything (decision 87).
- **The new tools container** (`wordpress:cli`, uid 33) runs WP-CLI only when someone uses it. It runs no cron and writes nothing by itself.
- **A verified, read-only full backup of every site comes first** (Step 0), before any site is changed.
- **Encryption stays off everywhere** until the binder work-factor fix is released.

## Current state

| Site | Status | DB | PHP | wp-config DB settings | Webroot + DB (raw) | Old backups |
| --- | --- | --- | --- | --- | --- | --- |
| eyecatcher | **done** (03:00) | MariaDB 11.4.12 | 8.4.27 | literal | 288M + 107M | new sidecar OK |
| itramblings | **done** (03:10) | MariaDB 11.4.12 | 8.4.27 | literal | 1.4G + 156M | new sidecar OK |
| karenandshawn | to do | MariaDB 11.4.12 | 8.4.27 | literal | 308M + 111M | cron failing (exit 127); last full backup unknown |
| spmsolutions | to do | MariaDB 11.4.12 | 8.4.27 | literal | 382M + 94M | failing (127) |
| ewecandoit | to do | MariaDB 11.4.12 | 8.4.27 | literal | 384M + 94M | failing (127) |
| preferredcontractingservices | to do | MariaDB 11.4.12 | 8.4.27 | literal | 414M + 169M | failing (127) |
| liquidlogiclabs | to do | MariaDB 11.4.12 | 8.4.27 | **getenv_docker** | 160M + 59M | failing (127) |
| enterprisearchai | to do | MariaDB 11.4.12 | 8.4.27 | **getenv_docker** | 566M + 129M | failing (126); its compose file is `compose.yml` |
| adamlehrhauptbeta | to do | MariaDB 11.4.12 | 8.4.24 | **getenv_docker** | 1.3G + 98M | failing ("Error creating database backup"); web image 7.0.4 |
| adamlehrhaupt | **owner decision** | MariaDB 11.4.12 | **7.4.29 (EOL)** | literal | 788M + 1.4G | failing (127); last full backup 2026-08-18 |

None of the 8 unconverted sites has a working scheduled backup: the old tools cron has failed every night for at least 14 days. Today they are protected only by manual snapshots.

## Step 0: a pre-change safety backup of all 10 sites (read-only, before any change)

For each site, with the published image `ghcr.io/liquidlogiclabs/l3io-wordpress-backup:1.1.1-mariadb@sha256:b75d716f…`:

1. **Create the folder:** `/mnt/backups-websites-wordpress/<site>-manual/prechange-20261114/`, owned 33:33 0750. This is outside the sidecar's `/backups`, so `prune` can never delete it.
2. **One-shot backup, read-only towards the site:**
   ```
   docker run --rm --network data -e PUID=33 -e PGID=33 -e TZ=Etc/UTC \
     -e L3IO_WP_BACKUP_SITE_NAME=<site> \
     [--env-file <the site's web .env>   # getenv_docker sites only, for WORDPRESS_DB_*]
     -v <webroot>:/var/www/html:ro \
     -v /mnt/backups-websites-wordpress/<site>-manual/prechange-20261114:/backups \
     --tmpfs /tmp/wpbackup:size=3g,mode=1777 \
     <image> backup
   ```
3. **`verify`** with the same image, read-only.
4. **Checks:** the Archive is 33:33 and passes `gzip -t`; the logs contain no password (compared by name or sha256 only, never printed).
5. **Record** the path, size, exit codes and duration for each site in a table.
6. **Stop rule:** if any site fails Step 0, nothing goes ahead for that site, and the failure is reported to Shawn.

Step 0 also proves the **getenv_docker** path on prod for the three sites that use it, before their sidecars exist.

## Per-site rollout (one site at a time, verified, stop at the first failure)

**Order:** simplest first.

| # | Site | Schedule (UTC) | Tools tag | Backup DB settings source |
| --- | --- | --- | --- | --- |
| 1 | karenandshawn | `20 3 * * *` | `wordpress:cli-2.12-php8.4` | wp-config (literal) |
| 2 | spmsolutions | `30 3 * * *` | cli-2.12-php8.4 | literal |
| 3 | ewecandoit | `40 3 * * *` | cli-2.12-php8.4 | literal |
| 4 | preferredcontractingservices | `50 3 * * *` | cli-2.12-php8.4 | literal |
| 5 | liquidlogiclabs | `0 4 * * *` | cli-2.12-php8.4 | **site `.env` `WORDPRESS_DB_*`** via env_file |
| 6 | enterprisearchai | `10 4 * * *` | cli-2.12-php8.4 | **env_file**; file is `compose.yml` |
| 7 | adamlehrhauptbeta | `20 4 * * *` | cli-2.12-php8.4 | **env_file** |
| 8 | adamlehrhaupt | `30 4 * * *` | per Shawn's decision | literal |

**Each site (the eyecatcher pattern):**
1. **Backups and folders:** back up the live `docker-compose.yml` (or `compose.yml`) and the tools `.env`, as `*.bak-wpbackup-<UTC>` with mode 600. Create `backup-state` and `/mnt/backups-websites-wordpress/<site>` (plus `<site>-manual`, already made in Step 0), all 33:33 0750.
2. **Repo:** in `docker-apps-config-servers/src/linux/wordpress/<site>/app.json`, replace the tools block with:
   - **tools:** `wordpress:cli-2.12-php8.4`, user 33:33, the `sleep` keep-alive, `/backups` = `<site>-manual`;
   - **backup:** root start, `PUID`/`PGID` 33, `TZ=Etc/UTC`, `L3IO_WP_BACKUP_SITE_NAME`, the schedule slot above, webroot read-only, the Archive folder, state.

   For a **getenv_docker site**, the backup container also gets the site's DB environment (the same `.env` the web container uses, or just its `WORDPRESS_DB_*` entries). Without it, the backup would fall back to the image's defaults and refuse.
3. **Prod:** hand-place the rendered tools and backup blocks in place of the tools block. Re-read the line numbers per site: it starts at line 51, 53, 54 or 67 depending on the site. Check `compose config`. Then `up -d --no-deps` tools and backup only.
4. **Verify:**
   - tools runs `wp --path=/var/www/html core version` as uid 33;
   - the backup crontab line has the right slot and `prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6`;
   - a one-shot backup gives `run_finished` exit 0, the Archive is 33:33 and passes `gzip -t`, and the logs contain no password;
   - the web and db containers have the same container ID and start time as before;
   - **site HTTP 200,** checked properly from inside the web container: `curl -s -o /dev/null -w '%{http_code}' -H 'Host: <site host>' -H 'X-Forwarded-Proto: https' http://localhost/`, or the public URL through Traefik. The earlier hand-made 127.0.0.1 check was broken.
5. **Repo commit:** local only, not pushed.

**Rollback per site,** if any verification fails:
- restore the `*.bak-wpbackup-<UTC>` compose and .env files;
- `docker rm -f wordpress-<site>-backup`;
- `up -d --no-deps wordpress-<site>-tools`, which brings back the old ravensorb tools container;
- check the site returns 200.

The web and db containers are never touched, so a rollback can't affect site data.

## adamlehrhaupt: decision needed

PHP 7.4.29 (EOL). Its WordPress core is 6.8.12 on disk, although the image tag says 5.9.3, and a plugin crashes WP-CLI. It has had no full backup since 2026-08-18.
- **(a) Recommended:** add the backup sidecar only, and keep its current tools container with the cron off. It gets working backups (and Step 0), and the sidecar doesn't need WP-CLI.
- **(b)** Do the full switch, with tools on a PHP 7.4 `wordpress:cli` tag, if one exists (not yet checked). WP-CLI may still crash there.
- **(c)** Leave it out: no backups at all.

## Capacity

- **Space:** `/mnt/backups-websites-wordpress` and `/mnt/backups-general` are the same 239G volume, with 182G free.
- **One Archive per site:** about 2.7–4.9G for all 10.
- **Retention of about 15 per site:** about 40–74G expected, about 140G in the worst case. That fits, but shares the space with everything else on Backup-General.
- **Watch:** check the share's free space after the first week of scheduled runs.
- **Later, separate:** clean up the old `/mnt/backups-general/wordpress/<site>` contents (57G used in total, much of it agent snapshots) once the new process has two weeks of history.

## After the rollout

1. Check each site's first scheduled `run_finished` the following night (it's in `docker logs`).
2. After a week, check that prune has kept the right tiers and removed nothing else.
3. The **1.1.2** release (CDN-race fix, plus the binder work-factor fix once released) moves the image tag. That's a separate change, one site first.
4. Only after everything has run cleanly, push the docker-apps-config-servers commits, after that repo's own CI passes.
5. The wordpress-estate skill already handles mixed sites: it checks each tools container's image to choose the old or new WP-CLI form.

## Approvals needed

1. The plan as a whole, including the Step 0 safety backups.
2. **adamlehrhaupt:** (a), (b) or (c).
3. Passing each getenv_docker site's `.env` to its backup container. That file holds the DB password; the backup reads it the way the web container does.
