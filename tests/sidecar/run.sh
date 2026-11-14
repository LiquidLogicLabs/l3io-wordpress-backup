#!/usr/bin/env bash
# Smoke run of the official image (docker/Dockerfile) as a Docker sidecar
# beside the official WordPress image, in both of its modes: one-shot
# (`docker compose exec backup wpbackup2 ...`) and scheduled (Supercronic under
# s6 when started as root, and directly when started as 33:33 with a read-only
# root; a crontab firing every minute must produce an Archive in each). It also
# checks that nothing re-owns the shared volumes (owner decision 87): another
# PUID is refused, with the fix, and the state and Archives keep their owner.
# Not part of the pytest suite; ci.yml's `image` job and docker.yml's smoke job
# run it.
#
#   tests/sidecar/run.sh mysql      # mysql:8.0
#   tests/sidecar/run.sh mysql84    # mysql:8.4
#   tests/sidecar/run.sh mariadb    # mariadb:11.4
#
# Environment:
#   IMAGE=REF                     test this image (a published one, by digest)
#                                 instead of building one from this checkout
#   L3IO_DB_CLIENT=mysql|mariadb  client family of the image built here
#                                 (default: mysql; docs/docker.md says why)
#   REMOVE_IMAGE=1                remove the image built here on exit
#   SKIP_BUILD=1                  reuse an image built here earlier
#   L3IO_IMAGE_SUFFIX=S           suffix for the image built here (parallel runs)
#   L3IO_SIDECAR_SCRATCH=DIR      parent of the scratch directory (default
#                                 $TMPDIR): must be the same path for this script
#                                 and the Docker daemon, which bind-mounts it
#   KEEP_STACK=1                  leave the stack and scratch dir up (debugging;
#                                 tear down with the `docker compose ... down -v` it prints)
#   L3IO_LOCAL_WHEELS=DIR         install the wheels in DIR over the published ones
#                                 (a family release not yet on PyPI, built from
#                                 its checkout; local runs only)
#
# Everything it creates -- containers, volumes, the network, the bind-mounted
# Site Root, generated credentials -- is removed on exit. Only the log
# directory under the scratch dir is kept; its path is printed last.
#
# Credentials are generated at runtime, written to 0600 env files in the
# scratch dir and never printed. The step table records each step's exit code
# against the one expected; `*` marks a probe whose outcome is reported, not
# asserted (only the fallback after a failed restore has any). The script
# exits 1 when any asserted step differs.

# SC2016: single-quoted $VARS are expanded inside the containers, on purpose.
# shellcheck disable=SC2016
set -Eeuo pipefail
umask 077

usage() { echo "usage: $0 mysql|mysql84|mariadb" >&2; exit 2; }
[[ $# -eq 1 ]] || usage
ENGINE=$1
[[ $ENGINE == mysql || $ENGINE == mysql84 || $ENGINE == mariadb ]] || usage

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
if [[ -n ${IMAGE:-} ]]; then
    BUILT=0
    docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null
    L3IO_DB_CLIENT=$(docker image inspect -f '{{index .Config.Labels "io.l3io.wp-backup.db-client"}}' "$IMAGE")
else
    BUILT=1
    L3IO_DB_CLIENT=${L3IO_DB_CLIENT:-mysql}
    IMAGE="l3io-wordpress-backup:sidecar-test-$L3IO_DB_CLIENT${L3IO_IMAGE_SUFFIX:+-$L3IO_IMAGE_SUFFIX}"
fi
L3IO_SIDECAR_IMAGE=$IMAGE

rand() { openssl rand -hex "$1"; }
L3IO_SCRATCH=$(mktemp -d -p "${L3IO_SIDECAR_SCRATCH:-${TMPDIR:-/tmp}}" l3io-sidecar.XXXXXX)
LOGS=$L3IO_SCRATCH/logs
mkdir -p "$LOGS" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/tls"
chmod 0755 "$L3IO_SCRATCH" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/tls"
PROJECT="l3io-sidecar-$ENGINE-$(rand 4)"
export L3IO_SCRATCH L3IO_DB_CLIENT L3IO_SIDECAR_IMAGE

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "$LOGS/run.log" >&2; }
dc() { docker compose -f "$HERE/compose.yaml" -p "$PROJECT" --profile "$ENGINE" "$@"; }

# ---- teardown ----------------------------------------------------------------
SECRETS=()
cleanup() {
    local rc=$?
    set +e
    log "teardown (exit $rc)"
    dc --profile tools --profile schedule logs --no-color --timestamps >"$LOGS/compose.log" 2>&1
    # Leak check: no generated secret may appear in any collected log.
    local leaks=0 s
    for s in "${SECRETS[@]}"; do
        grep -rqF -- "$s" "$LOGS" && leaks=$((leaks + 1))
    done
    log "secret leak check over $LOGS: $leaks of ${#SECRETS[@]} secrets found"
    if [[ ${KEEP_STACK:-0} == 1 ]]; then
        log "KEEP_STACK=1: left up; tear down with: L3IO_SCRATCH=$L3IO_SCRATCH L3IO_SIDECAR_IMAGE=$IMAGE docker compose -f $HERE/compose.yaml -p $PROJECT --profile $ENGINE --profile tools --profile schedule down -v"
        exit "$rc"
    fi
    dc --profile tools --profile schedule down -v --remove-orphans -t 5 >>"$LOGS/teardown.log" 2>&1
    # The Site Root is owned by www-data (33) inside the containers.
    if [[ -d $L3IO_SCRATCH/html ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        docker run --rm --user 0 -v "$L3IO_SCRATCH:/s" --entrypoint rm "$IMAGE" -rf /s/html
    fi
    rm -rf "$L3IO_SCRATCH/html" "$L3IO_SCRATCH/tls" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/ctx" \
        "$L3IO_SCRATCH"/*.env "$L3IO_SCRATCH/wp-config.docker.php" "$L3IO_SCRATCH/crontab"
    [[ ${REMOVE_IMAGE:-0} == 1 && $BUILT == 1 ]] && docker image rm "$IMAGE" >/dev/null 2>&1
    log "logs kept in $LOGS"
    exit "$rc"
}
trap cleanup EXIT

# ---- generated throwaway credentials ----------------------------------------
TAG=$(rand 4)
DB_ROOT_PW=$(rand 24); DB_PW=$(rand 24); WP_ADMIN_PW=$(rand 24)
PASSPHRASE=$(rand 24); WRONG_PASSPHRASE=$(rand 24)
DB_NAME="wpdb_$TAG"; DB_USER="wpu_$TAG"
SENTINEL="l3io-sentinel-$(rand 8)"
SECRETS=("$DB_ROOT_PW" "$DB_PW" "$WP_ADMIN_PW" "$PASSPHRASE" "$WRONG_PASSPHRASE")
# The scheduled mode's prune (owner decisions 83, 84): one daily, the image's
# weekly and monthly tiers off. Every Archive of the run is of one UTC day (or
# two, across midnight), so exactly one of this site's stays: the newest.
SCHEDULED_KEEP_DAILY=1
# The scheduled backups write into subfolders (owner decision 86); every
# earlier Archive of the run is flat, so the prune and `latest` must read both.
SCHEDULED_SUBDIR='{site}/{yyyy}/{mm}'

cat >"$L3IO_SCRATCH/db.env" <<EOF
MYSQL_ROOT_PASSWORD=$DB_ROOT_PW
MYSQL_DATABASE=$DB_NAME
MYSQL_USER=$DB_USER
MYSQL_PASSWORD=$DB_PW
MARIADB_ROOT_PASSWORD=$DB_ROOT_PW
MARIADB_DATABASE=$DB_NAME
MARIADB_USER=$DB_USER
MARIADB_PASSWORD=$DB_PW
EOF
cat >"$L3IO_SCRATCH/wp.env" <<EOF
WORDPRESS_DB_HOST=db
WORDPRESS_DB_NAME=$DB_NAME
WORDPRESS_DB_USER=$DB_USER
WORDPRESS_DB_PASSWORD=$DB_PW
EOF
# No administrator: a restore loads the site's own database with the site's
# own account (owner decision 55). The file stays, for settings a step adds.
cat >"$L3IO_SCRATCH/backup.env" <<EOF
# no administrator (owner decision 55)
EOF
# The schedule phase: the image's daily expression replaced by one firing every
# minute, and a mounted crontab whose job is merged in (yearly: it does not fire).
# Not an impossible date (31 Feb): Supercronic 0.2.51 accepts one and then
# logs a warning in a busy loop.
cat >"$L3IO_SCRATCH/schedule.env" <<EOF
WPBACKUP_SCHEDULE=* * * * *
WPBACKUP_KEEP_DAILY=$SCHEDULED_KEEP_DAILY
WPBACKUP_KEEP_WEEKLY=0
WPBACKUP_KEEP_MONTHLY=0
L3IO_WP_BACKUP_ARCHIVE_SUBDIR=$SCHEDULED_SUBDIR
EOF
printf '%s\n' '# mounted by tests/sidecar/run.sh' '0 0 1 1 * wpbackup2 doctor' >"$L3IO_SCRATCH/crontab"
chmod 0644 "$L3IO_SCRATCH/crontab"

# Test CA and a server certificate for `db`, for the TLS opt-in steps
# (--db-tls; no TLS is required by default, owner decision 56).
(
    cd "$L3IO_SCRATCH/tls"
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=l3io sidecar test CA $TAG" \
        -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout ca-key.pem -out ca.pem 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj "/CN=db" -keyout server-key.pem -out server.csr 2>/dev/null
    # Python 3.13+ verifies with VERIFY_X509_STRICT: the CA needs keyUsage and
    # the leaf an Authority Key Identifier, or verification fails.
    printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature,keyEncipherment' \
        'extendedKeyUsage=serverAuth' 'subjectAltName=DNS:db' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer' >san.ext
    openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 2 \
        -extfile san.ext -out server.pem 2>/dev/null
    rm -f ca-key.pem server.csr san.ext ca.srl
    chmod 0644 ./*.pem   # the db container's mysql user (999) reads them; throwaway
)

printf '%s\nuploaded by tests/sidecar/run.sh\n' "$SENTINEL" >"$L3IO_SCRATCH/content/$SENTINEL.txt"
chmod 0644 "$L3IO_SCRATCH/content/$SENTINEL.txt"
UPLOAD_SHA=$(sha256sum "$L3IO_SCRATCH/content/$SENTINEL.txt" | cut -c1-64)
mkdir -m 0755 "$L3IO_SCRATCH/html"

# ---- step bookkeeping ---------------------------------------------------------
RESULTS=()
FAILED=0
LAST_RC=0
# step NAME EXPECTED_RC CMD...   (EXPECTED_RC `*` = probe, reported only)
step() {
    local name=$1 want=$2 rc t0 t1
    shift 2
    log "step $name (expect $want)"
    t0=$(date +%s%N)
    set +e
    "$@" >"$LOGS/$name.out" 2>"$LOGS/$name.err"
    rc=$?
    set -e
    LAST_RC=$rc
    t1=$(date +%s%N)
    local secs
    secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.1f", (b - a) / 1e9 }')
    local verdict=ok
    if [[ $want != '*' && $rc != "$want" ]]; then
        verdict=MISMATCH
        FAILED=1
        # The step's own output lives in $LOGS, which CI discards with the runner:
        # show its tail here so a failure is diagnosable from the job log. The
        # values in it are this run's throwaway test credentials, never real ones.
        { echo "---- $name: last lines of stderr ----"; tail -n 25 "$LOGS/$name.err"; } >&2 || true
    fi
    [[ $want == '*' ]] && verdict=probe
    RESULTS+=("$(printf '%-34s want=%-2s got=%-3s %6ss  %s' "$name" "$want" "$rc" "$secs" "$verdict")")
    log "  -> rc=$rc (${secs}s) $verdict"
    return 0
}
check() {  # check NAME CONDITION-CMD...  (a yes/no fact about the Site)
    local name=$1
    shift
    if "$@" >>"$LOGS/checks.log" 2>&1; then
        RESULTS+=("$(printf '%-34s %s' "check:$name" ok)")
    else
        RESULTS+=("$(printf '%-34s %s' "check:$name" FAILED)"); FAILED=1
    fi
}

wb() { dc exec -T backup wpbackup2 --log-format json "$@"; }
wb_pp() { L3IO_WP_BACKUP_PASSPHRASE=$PASSPHRASE dc exec -T -e L3IO_WP_BACKUP_PASSPHRASE backup wpbackup2 --log-format json "$@"; }
wb_wrong() { L3IO_WP_BACKUP_PASSPHRASE=$WRONG_PASSPHRASE dc exec -T -e L3IO_WP_BACKUP_PASSPHRASE backup wpbackup2 --log-format json "$@"; }
wb_root_pp() {
    L3IO_WP_BACKUP_PASSPHRASE=$PASSPHRASE L3IO_WP_BACKUP_MODE=files \
        dc exec -T -u 0 -e L3IO_WP_BACKUP_PASSPHRASE -e L3IO_WP_BACKUP_MODE backup wpbackup2 --log-format json "$@"
}
wb_dbenv() {
    L3IO_WP_BACKUP_DB_USER=$DB_USER L3IO_WP_BACKUP_DB_PASSWORD=$DB_PW \
        dc exec -T -e L3IO_WP_BACKUP_DB_USER -e L3IO_WP_BACKUP_DB_PASSWORD backup wpbackup2 --log-format json "$@"
}
wp() { dc run --rm -T wpcli wp --path=/var/www/html "$@"; }
http_code() { dc run --rm -T curl -sL -o /dev/null -w '%{http_code}' "http://wordpress$1"; }
http_body() { dc run --rm -T curl -sL "http://wordpress$1"; }
in_backup() { dc exec -T backup "$@"; }
db_lines() { in_backup grep -E "define\\( *'DB_(NAME|HOST|USER)'" "$SITE/wp-config.php"; }
absent_in_site() { ! in_backup test -e "$SITE/$1"; }
absent_in_backups() { ! in_backup test -e "/backups/$1"; }
code_is() { [[ $(http_code "$1") == "$2" ]]; }
no_foreign_owner() { [[ -z $(in_backup find "$SITE" ! -uid 33 -print -quit) ]]; }

SITE=/var/www/html
CA=/run/l3io-tls/ca.pem

# ---- build -----------------------------------------------------------------------
log "engine=$ENGINE image=$IMAGE client=$L3IO_DB_CLIENT project=$PROJECT scratch=$L3IO_SCRATCH"
log "repo HEAD $(git -C "$REPO" rev-parse --short HEAD); modified tracked files: $(git -C "$REPO" status --porcelain --untracked-files=no | wc -l)"
if [[ $BUILT == 1 && ${SKIP_BUILD:-0} != 1 ]]; then
    # The product Dockerfile, with this checkout's wheel (and L3IO_LOCAL_WHEELS)
    # in its test-only `wheels` context in place of the PyPI release.
    mkdir -p "$L3IO_SCRATCH/ctx"
    step build-wheel 0 bash -c "cd '$REPO' && uv build --no-sources --wheel -o '$L3IO_SCRATCH/ctx'"
    if [[ -n ${L3IO_LOCAL_WHEELS:-} ]]; then
        cp "$L3IO_LOCAL_WHEELS"/*.whl "$L3IO_SCRATCH/ctx/"
        log "local wheels: $(cd "$L3IO_SCRATCH/ctx" && echo ./*.whl)"
    fi
    VERSION=$(basename "$(ls "$L3IO_SCRATCH"/ctx/l3io_wordpress_backup-*.whl)" | cut -d- -f2)
    step build-image 0 docker buildx build --load -f "$REPO/docker/Dockerfile" \
        --build-context "wheels=$L3IO_SCRATCH/ctx" --build-arg "VERSION=$VERSION" \
        --build-arg "DB_CLIENT=$L3IO_DB_CLIENT" -t "$IMAGE" "$REPO/docker"
    [[ $FAILED == 0 ]] || { printf '%s\n' "${RESULTS[@]}"; exit 1; }
fi

# ---- the image's entrypoint, without the stack ---------------------------------
img() { docker run --rm "$@"; }
# A wpbackup2 subcommand naming an executable on PATH would run that executable
# instead (l3io-wpbackup-entrypoint). The subcommands are read from the image's
# own wpbackup2, never listed here.
subcommands_are_not_executables() {
    local subs s
    subs=$(img "$IMAGE" --help | sed -n 's/^ *{\([a-z,-]*\)}.*/\1/p' | head -1 | tr , ' ')
    echo "subcommands: $subs"
    [[ -n $subs ]] || return 1
    for s in $subs; do
        if img --entrypoint sh "$IMAGE" -c "command -v $s"; then return 1; fi
    done
}
one_shot_is_wpbackup2() { [[ $(img "$IMAGE" --version) == "wpbackup2 "* ]]; }
one_shot_runs_as_33() { [[ $(img "$IMAGE" id -u):$(img "$IMAGE" id -g) == 33:33 ]]; }
# Owner decision 87: PUID:PGID is who runs; nothing is re-owned for it (the
# image's state folder stays 33:33 0700).
puid_pgid_honoured() {
    local got
    got=$(img -e PUID=1234 -e PGID=1234 "$IMAGE" sh -c 'echo "$(id -u):$(id -g) $(stat -c %u:%g:%a "$XDG_STATE_HOME")"')
    echo "PUID=PGID=1234: $got"
    [[ $got == "1234:1234 33:33:700" ]]
}
puid_0_keeps_root() { [[ $(img -e PUID=0 "$IMAGE" id -u) == 0 ]]; }
# Docker's --user: that user, directly; PUID/PGID naming another is refused.
user_flag_honoured() { [[ $(img --user 1234:1234 "$IMAGE" id -u):$(img --user 1234:1234 "$IMAGE" id -g) == 1234:1234 ]]; }
user_flag_puid_mismatch_refused() {
    local out rc=0
    out=$(img --user 33:33 -e PUID=1000 "$IMAGE" --version 2>&1) || rc=$?
    echo "rc=$rc: $out"
    [[ $rc == 2 && $out == *"cannot switch users"* ]]
}
# A scheduled start whose user cannot use the image's own data folders (33:33
# 0700, no volume) stops at start with the fix, as root (s6) and as --user.
scheduled_start_refused() {
    local out rc=0
    out=$(timeout 60 docker run --rm "$@" "$IMAGE" 2>&1) || rc=$?
    echo "rc=$rc"
    grep -E 'is not (writable|readable)|never changes ownership|not started' <<<"$out" || true
    [[ $rc != 0 && $out == *"wpbackup2 never changes ownership"* && $out == *"/var/lib/wpbackup/state"* ]]
}
# The scheduled mode refuses to start (s6 stops the container, non-zero) on an
# empty or unparsable schedule rather than idling with nothing scheduled.
schedule_refused() { ! timeout 60 docker run --rm -e "WPBACKUP_SCHEDULE=$1" "$IMAGE"; }
keep_refused() { ! timeout 60 docker run --rm -e "$1=$2" "$IMAGE"; }
# The image's default retention (owner decision 84): the job the defaults build.
default_retention_job() {
    local job
    job=$(img "$IMAGE" sh -c 'l3io-wpbackup-crontab "$TMPDIR/c" >/dev/null && cat "$TMPDIR/c"')
    echo "default job: $job"
    [[ $job == "0 3 * * * wpbackup2 backup && wpbackup2 prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6" ]]
}
check image:subcommands-are-not-executables subcommands_are_not_executables
check image:one-shot-is-wpbackup2 one_shot_is_wpbackup2
check image:one-shot-runs-as-33 one_shot_runs_as_33
check image:puid-pgid-honoured puid_pgid_honoured
check image:puid-0-keeps-root puid_0_keeps_root
check image:user-flag-honoured user_flag_honoured
check image:user-flag-puid-mismatch-refused user_flag_puid_mismatch_refused
check image:scheduled-other-puid-refused-at-start scheduled_start_refused -e PUID=1234 -e PGID=1234
check image:scheduled-user-flag-refused-at-start scheduled_start_refused --user 1234:1234
check image:empty-schedule-refused schedule_refused ''
check image:bad-schedule-refused schedule_refused 'not a schedule'
check image:bad-keep-refused keep_refused WPBACKUP_KEEP 'two weeks'
check image:negative-keep-refused keep_refused WPBACKUP_KEEP '-1'
check image:bad-keep-daily-refused keep_refused WPBACKUP_KEEP_DAILY 'seven'
check image:bad-keep-yearly-refused keep_refused WPBACKUP_KEEP_YEARLY '1.5'
check image:default-retention-is-7-4-6 default_retention_job

# ---- stack -----------------------------------------------------------------------
step stack-up 0 dc up -d --wait --wait-timeout 300
[[ $FAILED == 0 ]] || { printf '%s\n' "${RESULTS[@]}"; exit 1; }
in_backup sh -c 'id; wpbackup2 --version; age --version; (mysqldump --version || mariadb-dump --version) 2>&1; ls -ld /var/www/html /backups "$XDG_STATE_HOME" "$TMPDIR"' \
    >"$LOGS/sidecar-env.txt" 2>&1 || true
step passphrase-passthrough 0 env L3IO_WP_BACKUP_PASSPHRASE="$PASSPHRASE" \
    docker compose -f "$HERE/compose.yaml" -p "$PROJECT" --profile "$ENGINE" \
    exec -T -e L3IO_WP_BACKUP_PASSPHRASE backup sh -c 'test -n "$L3IO_WP_BACKUP_PASSPHRASE"'

# ---- WordPress install and content ---------------------------------------------
# `up --wait` returns once the containers report healthy, which can be before the
# official image has written wp-config.php or the database accepts the site's
# user; wp-install then failed in 0.5 s (CI, 2026-11-14). Wait for both, bounded.
# The probe goes through PHP (mysqli), as WordPress does: `wp db query` would use
# the cli image's MariaDB client, which cannot authenticate against MySQL 8's
# caching_sha2_password, so it never succeeded on the mysql legs. Before install,
# `wp core is-installed` exits 1 without an error; an unreachable database prints
# "Error establishing a database connection".
wordpress_ready() {
    local _ err
    for _ in $(seq 1 60); do
        if in_backup test -s "$SITE/wp-config.php"; then
            err=$(wp core is-installed 2>&1 >/dev/null) || true
            if [[ $err != *"database connection"* && $err != *"Error:"* ]]; then
                return 0
            fi
        fi
        sleep 2
    done
    printf 'last probe stderr: %s\n' "${err:-<wp-config.php missing>}" >&2
    return 1
}
step wordpress-ready 0 wordpress_ready
step wp-install 0 wp core install --url=http://wordpress --title="l3io sidecar $TAG" \
    --admin_user="admin_$TAG" --admin_password="$WP_ADMIN_PW" --admin_email="admin-$TAG@example.invalid" --skip-email
POST_ID=$(wp post create --post_title="Post $SENTINEL" --post_content="Body $SENTINEL" \
    --post_status=publish --porcelain 2>>"$LOGS/wp-content.err")
ATTACH_ID=$(wp media import "/content/$SENTINEL.txt" --title="Upload $SENTINEL" --porcelain 2>>"$LOGS/wp-content.err")
UPLOAD_REL=$(wp post meta get "$ATTACH_ID" _wp_attached_file 2>>"$LOGS/wp-content.err")
log "post $POST_ID, attachment $ATTACH_ID at uploads/$UPLOAD_REL"
upload_matches() { [[ $(in_backup sha256sum "$SITE/wp-content/uploads/$UPLOAD_REL" | cut -c1-64) == "$UPLOAD_SHA" ]]; }
# Captured first: `cmd | grep -q` under pipefail fails when grep exits early.
post_has_sentinel() { local c; c=$(wp post get "$POST_ID" --field=post_content) && [[ $c == *"$SENTINEL"* ]]; }
page_serves_sentinel() {
    local b
    local code
    code=$(http_code "/?p=$POST_ID") || true
    b=$(http_body "/?p=$POST_ID") || true
    echo "GET /?p=$POST_ID -> $code, ${#b} bytes, sentinel $([[ $b == *"$SENTINEL"* ]] && echo found || echo absent)"
    [[ $code == 200 && $b == *"$SENTINEL"* ]]
}
check before:upload-present upload_matches
check before:post-present post_has_sentinel
check before:page-200-with-sentinel page_serves_sentinel

# ---- Phase A: the official image's getenv_docker() wp-config.php, as deployed ----
# The sidecar carries the site's WORDPRESS_DB_* (compose: wp.env), so the
# getenv_docker() DB_* and $table_prefix resolve as PHP resolves them (owner
# decision 47). The image sets L3IO_WP_BACKUP_WP_DIR and
# L3IO_WP_BACKUP_ARCHIVE_DIR (owner decision 60), so no flag names the site or
# the Archive, and the database host is logged, never confirmed (decision 54).
db_lines >"$LOGS/A-wp-config-db-lines.txt" || true
cp "$L3IO_SCRATCH/html/wp-config.php" "$L3IO_SCRATCH/wp-config.docker.php"
no_archives() { [[ -z $(in_backup find /backups -name '*.tar.gz' -print -quit) ]]; }
logged() { grep -qF "\"event\": \"$2\"" "$LOGS/$1.err"; }
printed() { grep -qF -- "$2" "$LOGS/$1.out"; }
step A-doctor 0 wb doctor
step A-backup-dryrun 0 wb --whatIf backup
check A:dryrun-wrote-nothing no_archives
check A:dryrun-names-the-host printed A-backup-dryrun "from db:3306"
step A-backup 0 wb backup
check A:host-logged logged A-backup database_host
# Owner decision 82: wpbak-<site>-<YYYYMMDDTHHMMSSZ>.tar.gz, <site> the
# database name when --site-name is not set.
default_named() { grep -qE "^/backups/wpbak-$DB_NAME-[0-9]{8}T[0-9]{6}Z\.tar\.gz\$" "$LOGS/A-backup.out"; }
check A:default-name-is-wpbak-db-utc default_named
step A-verify 0 wb verify --archive "$(head -1 "$LOGS/A-backup.out")"
# The dump client against this server: client_server_skew (its event and code since
# l3io-wordpress-database 0.3.6, the text in `message`) exactly when the client's family
# is not the server's (docs/docker.md: which variant to pick).
skew_logged() { grep -qF '"code": "client_server_skew"' "$LOGS/A-backup.err"; }
skew_is_an_event_name() {
    ! grep -qE '"event": "[^"]*older than' "$LOGS/A-backup.err" &&
        { ! skew_logged || grep -qF '"event": "client_server_skew"' "$LOGS/A-backup.err"; }
}
server_family=mysql; [[ $ENGINE == mariadb ]] && server_family=mariadb
if [[ $L3IO_DB_CLIENT == "$server_family" ]]; then
    no_skew_logged() { ! skew_logged; }
    check A:no-client-server-skew no_skew_logged
else
    check A:client-server-skew-logged skew_logged
fi
check A:skew-event-is-a-name skew_is_an_event_name
# --archive latest chooses by Manifest across the legacy and the new names: a
# legacy-named Archive written after the default one is the newest (though
# `wpbackup2-` sorts before `wpbak-`), then a --site-name one newer still.
sleep 1  # created_at has whole seconds
LEGACY=/backups/wpbackup2-$DB_NAME-20261103184135.tar.gz
step A-backup-legacy-name 0 wb backup --archive "$LEGACY"
step A-latest-legacy 0 wb --whatIf restore --archive latest --mode files
check A:latest-is-the-legacy-named-archive printed A-latest-legacy "archive latest: $LEGACY"
sleep 1
step A-backup-site-name 0 wb backup --site-name "Sidecar Site"
check A:site-name-names-the-archive printed A-backup-site-name "/backups/wpbak-Sidecar-Site-"
step A-latest-site-name 0 wb --whatIf restore --archive latest --mode files
check A:latest-is-the-site-named-archive printed A-latest-site-name "archive latest: $(head -1 "$LOGS/A-backup-site-name.out")"
step A-backup-dbenv 0 wb_dbenv backup --archive /backups/a2.tar.gz
step A-verify-dbenv 0 wb verify --archive /backups/a2.tar.gz

# ---- Phase B: DB_* written as literals (wp config set), then the full cycle ------
step B-literal-config 0 dc run --rm -T wpcli sh -c '
    set -eu
    wp --path=/var/www/html config set DB_HOST "$WORDPRESS_DB_HOST" --quiet
    wp --path=/var/www/html config set DB_NAME "$WORDPRESS_DB_NAME" --quiet
    wp --path=/var/www/html config set DB_USER "$WORDPRESS_DB_USER" --quiet
    wp --path=/var/www/html config set DB_PASSWORD "$WORDPRESS_DB_PASSWORD" --quiet
    wp --path=/var/www/html config set table_prefix wp_ --type=variable --quiet'
db_lines >"$LOGS/B-wp-config-db-lines.txt" || true
check B:page-200-after-literal page_serves_sentinel

step B-doctor 0 wb doctor
step B-backup-dryrun 0 wb --whatIf backup
step B-backup 0 wb backup
step B-verify-plain 0 wb verify --archive "$(head -1 "$LOGS/B-backup.out")"
sleep 1  # created_at has whole seconds: enc.tar.gz is the newest
step B-backup-encrypted 0 wb_pp backup --archive /backups/enc.tar.gz --encrypt-with-passphrase
step B-verify-encrypted 0 wb verify --archive /backups/enc.tar.gz
step B-verify-encrypted-auth 0 wb_pp verify --archive /backups/enc.tar.gz --authenticate
step B-verify-wrong-passphrase 8 wb_wrong verify --archive /backups/enc.tar.gz --authenticate
in_backup sh -c 'ls -ln /backups; stat -c "%n %s bytes mode %a uid %u" /backups/*.tar.gz' >"$LOGS/B-archives.txt" 2>&1 || true
in_backup sh -c 'tar -tzf /backups/enc.tar.gz | head -20; tar -tzf /backups/enc.tar.gz | wc -l' >"$LOGS/B-enc-members.txt" 2>&1 || true

# Alter the Site: delete the post and the attachment (WordPress removes the file).
step B-alter-delete-post 0 wp post delete "$POST_ID" --force
step B-alter-delete-upload 0 wp post delete "$ATTACH_ID" --force
check altered:upload-gone absent_in_site "wp-content/uploads/$UPLOAD_REL"
check altered:page-404 code_is "/?p=$POST_ID" 404

# No administrator and no --force: the site's own account loads its own
# database (owner decision 55), and neither a WordPress Site Root nor a
# WordPress database needs --force (decision 57). No TLS is required (decision
# 56); the database's own certificate is exercised by the opt-in steps.
RESTORE=(restore --archive latest)
db_sql() {
    local client=mysql
    [[ $ENGINE == mariadb ]] && client=mariadb
    dc exec -T "db-$ENGINE" sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" $client -uroot" <<<"$1"
}
# A plugin's table added after the backup (the site's prefix: the restore
# drops it, owner decision 53) and another application's table in the same
# database (no prefix: never touched).
PLUGIN_TABLE="wp_l3io_plugin_$TAG"
OTHER_TABLE="other_app_$TAG"
table_present() { [[ $(db_sql "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB_NAME' AND TABLE_NAME='$1';" | tail -1) == 1 ]]; }
table_absent() { ! table_present "$1"; }
step B-plant-tables 0 db_sql "CREATE TABLE \`$DB_NAME\`.\`$PLUGIN_TABLE\` (id INT PRIMARY KEY); CREATE TABLE \`$DB_NAME\`.\`$OTHER_TABLE\` (id INT PRIMARY KEY); INSERT INTO \`$DB_NAME\`.\`$OTHER_TABLE\` VALUES (7);"
check B:plugin-table-planted table_present "$PLUGIN_TABLE"
db_sql "SHOW GRANTS FOR '$DB_USER'@'%';" >"$LOGS/B-wordpress-account-grants.txt" 2>&1 || true
step B-restore-dryrun 0 wb_pp --whatIf "${RESTORE[@]}"
check B:dryrun-names-latest printed B-restore-dryrun "archive latest: /backups/enc.tar.gz"
check B:latest-logged logged B-restore-dryrun archive_latest_selected
check B:dryrun-changed-nothing absent_in_site "wp-content/uploads/$UPLOAD_REL"
check B:dryrun-names-the-live-database printed B-restore-dryrun "into the live database $DB_NAME on db:3306"
check B:dryrun-names-the-plugin-table printed B-restore-dryrun "$PLUGIN_TABLE"
check B:dryrun-kept-the-plugin-table table_present "$PLUGIN_TABLE"
step B-restore-dryrun-tls-verified 0 wb_pp --whatIf "${RESTORE[@]}" --db-tls verify-identity --db-tls-ca "$CA"
step B-restore-dryrun-tls-untrusted-ca 3 wb_pp --whatIf "${RESTORE[@]}" --db-tls verify-identity
step B-restore-dryrun-tls-required 0 wb_pp --whatIf "${RESTORE[@]}" --db-tls required
# The image carries no WP-CLI (docs/docker.md): a URL relocation is refused
# before any effect, exit 7 NOT_READY naming `wp`.
step B-relocation-needs-wp-cli 7 wb_pp --whatIf "${RESTORE[@]}" --new-site-url http://relocated.invalid
check B:relocation-names-wp-cli grep -qF 'wp is needed for SEARCH_REPLACE' "$LOGS/B-relocation-needs-wp-cli.err"
step B-restore 0 wb_pp "${RESTORE[@]}"
run_id_of() { jq -r 'select(.event == "run_started") | .l3io_run_id' "$LOGS/$1.err" 2>/dev/null | head -1; }
if [[ $LAST_RC != 0 ]]; then
    # Fallback, so the rest of the restore path is still exercised: resolve the
    # interrupted Run, try the database alone without a Safety Snapshot (does a
    # failed load leave the live site intact?), resolve again, then restore the
    # files alone (no database connection).
    step B-resolve-1 '*' wb_pp "${RESTORE[@]}" --resolve-interrupted-run "$(run_id_of B-restore)"
    step B-restore-db-no-snapshot '*' wb_pp restore --archive /backups/enc.tar.gz --mode db \
        --force --no-safety-snapshot
    check after-failed-db-load:home-200 code_is / 200
    step B-resolve-2 '*' wb_pp "${RESTORE[@]}" --resolve-interrupted-run "$(run_id_of B-restore-db-no-snapshot)"
    step B-restore-files 0 wb_pp restore --archive /backups/enc.tar.gz --mode files
fi
db_lines >"$LOGS/B-wp-config-after-restore.txt" || true
in_backup sh -c 'find "$XDG_STATE_HOME" -maxdepth 4 -printf "%M %u %p\n"' >"$LOGS/B-state-tree.txt" 2>&1 || true
check restored:upload-back upload_matches
check restored:post-back post_has_sentinel
check restored:page-200-with-sentinel page_serves_sentinel
check restored:home-200 code_is / 200
check restored:files-owned-by-33 no_foreign_owner
check restored:plugin-table-gone table_absent "$PLUGIN_TABLE"
check restored:other-table-kept table_present "$OTHER_TABLE"
check restored:dropped-tables-logged logged B-restore database_tables_dropped
check restored:db-name-unchanged in_backup grep -qE "define\\( *'DB_NAME', *'$DB_NAME'" "$SITE/wp-config.php"
check restored:no-administrator-used printed B-restore "\"administrator\": false"

# A restore started as root, with no flag but --archive (the Mode from the
# environment): it reads the passphrase and the Archive, then runs as the Site
# Root's owner, www-data (owner decision 58). A WordPress Site Root needs no
# --force (decision 57). DB_NAME did not change (owner decision 49), so
# --archive latest still finds this site's Archives after a database restore.
step B-latest-after-restore 0 wb_pp --whatIf restore --archive latest --mode files
check B:latest-after-restore-found printed B-latest-after-restore "archive latest: /backups/"
step B-restore-files-as-root 0 wb_root_pp restore --archive /backups/enc.tar.gz
check root:switched-to-the-owner logged B-restore-files-as-root restore_runs_as_site_owner
check root:files-owned-by-33 no_foreign_owner
check root:page-200-with-sentinel page_serves_sentinel
step B-backup-after-restore 0 wb backup --archive /backups/after.tar.gz
# A backup started as root, with no flag but the Archive's name: it reads its
# credentials as root, then runs as the Site Root's owner, www-data (owner
# decision 67), so the Archive is 33's and the Run shares the restore's state.
wb_root() { dc exec -T -u 0 backup wpbackup2 --log-format json "$@"; }
owned_by_33() { [[ $(in_backup stat -c %u:%g "$1") == 33:33 ]]; }
step B-backup-as-root 0 wb_root backup --archive /backups/root.tar.gz
check root:backup-switched-to-the-owner logged B-backup-as-root backup_runs_as_site_owner
check root:archive-owned-by-33 owned_by_33 /backups/root.tar.gz
step B-verify-root-backup 0 wb verify --archive /backups/root.tar.gz

db_sql "SHOW DATABASES;" >"$LOGS/databases-after-restore.txt" 2>&1 || true

# ---- Phase C: a restore onto the getenv_docker() config --------------------------
# A database restore loads straight into the database the environment names
# and leaves the getenv_docker() wp-config.php as it is (owner decisions 47,
# 49): the post deleted again is back, and the page serves 200, with no flag
# but the Archive. --overwrite-wp-config in a files-only Mode carries the target's
# getenv_docker() DB_* into the Archive's wp-config.php as written (owner
# decision 48): enc.tar.gz holds Phase B's file, which still defines the
# image's getenv_docker() helper, so it proceeds and the site keeps following
# its environment. A files-only restore keeping the target's file resolves the
# lookups and proceeds. An Archive taken from a plain (non-Docker) config does
# not define getenv_docker(): --overwrite-wp-config from it is refused (exit 3,
# UndefinedConfigFunctionError), the target's file untouched. None of these
# needs --force: the Site Root looks like WordPress (decision 57).
put_docker_config() {
    docker run --rm --user 0 -v "$L3IO_SCRATCH:/s" --entrypoint sh "$IMAGE" -c \
        'cp /s/wp-config.docker.php /s/html/wp-config.php && chown 33:33 /s/html/wp-config.php'
}
site_config_is_docker() {
    docker run --rm --user 0 -v "$L3IO_SCRATCH:/s" --entrypoint sh "$IMAGE" -c \
        '[ "$(sha256sum </s/wp-config.docker.php)" = "$(sha256sum </s/html/wp-config.php)" ]'
}
db_all_getenv_docker() {
    local n
    n=$(in_backup grep -cE "define\\( *'DB_(NAME|HOST|USER|PASSWORD)', *getenv_docker\\('WORDPRESS_DB_(NAME|HOST|USER|PASSWORD)'" \
        "$SITE/wp-config.php") || return 1
    echo "getenv_docker DB_* defines: $n"
    [[ $n == 4 ]]
}
php_db_name_is_env() {
    local n
    n=$(wp eval 'echo DB_NAME;') || return 1
    echo "PHP DB_NAME is the environment's: $([[ $n == "$DB_NAME" ]] && echo yes || echo no)"
    [[ $n == "$DB_NAME" ]]
}
refused_with() { grep -qF "$2" "$LOGS/$1.err"; }
put_docker_config
ENC=(restore --archive /backups/enc.tar.gz --require-encrypted)
step C-alter-delete-post 0 wp post delete "$POST_ID" --force
check C:altered-page-404 code_is "/?p=$POST_ID" 404
step C-restore-dryrun-docker-config 0 wb_pp --whatIf "${ENC[@]}"
check C:dryrun-left-config site_config_is_docker
step C-restore-docker-config 0 wb_pp "${ENC[@]}"
check C:restore-kept-getenv-docker-config site_config_is_docker
check C:restored-post-back post_has_sentinel
check C:restored-page-200-with-sentinel page_serves_sentinel
OVERWRITE=("${ENC[@]}" --mode files --overwrite-wp-config)
step C-overwrite-dryrun-docker-config 0 wb_pp --whatIf "${OVERWRITE[@]}"
check C:overwrite-dryrun-changed-nothing site_config_is_docker
step C-overwrite-docker-config 0 wb_pp "${OVERWRITE[@]}"
db_lines >"$LOGS/C-wp-config-after-overwrite.txt" || true
check C:overwrite-kept-getenv-docker-db db_all_getenv_docker
check C:overwrite-landed-archives-file in_backup grep -qF "\$table_prefix = 'wp_';" "$SITE/wp-config.php"
# PHP itself loads the file (so getenv_docker() is defined) and resolves DB_NAME
# from the environment, where the restored data is (owner decision 49).
check C:overwrite-php-resolves-env-db-name php_db_name_is_env
check C:overwrite-home-200 code_is / 200
check C:overwrite-page-200-with-sentinel page_serves_sentinel
put_docker_config
step C-restore-files-docker-config 0 wb_pp "${ENC[@]}" --mode files
check C:wp-config-still-getenv-docker in_backup grep -qF "getenv_docker('WORDPRESS_DB_NAME'" "$SITE/wp-config.php"
check C:page-200-after-files code_is / 200

# A plain config (wp config create: literal DB_*, no getenv_docker() helper),
# backed up, then that Archive's file offered over the Docker config.
step C-plain-config 0 dc run --rm -T wpcli sh -c '
    set -eu
    wp --path=/var/www/html config create --dbname="$WORDPRESS_DB_NAME" --dbuser="$WORDPRESS_DB_USER" \
        --dbpass="$WORDPRESS_DB_PASSWORD" --dbhost="$WORDPRESS_DB_HOST" --skip-check --skip-salts --force --quiet
    ! grep -q getenv_docker /var/www/html/wp-config.php'
step C-backup-plain-config 0 wb backup --archive /backups/plain-config.tar.gz
put_docker_config
PLAIN=(restore --archive /backups/plain-config.tar.gz --mode files --overwrite-wp-config)
step C-overwrite-dryrun-plain-archive 3 wb --whatIf "${PLAIN[@]}"
step C-overwrite-plain-archive 3 wb "${PLAIN[@]}"
check C:plain-refused-undefined-function refused_with C-overwrite-plain-archive UndefinedConfigFunctionError
check C:plain-refusal-left-config site_config_is_docker
check C:page-200-after-plain-refusal code_is / 200
step C-backup-after 0 wb backup --archive /backups/c.tar.gz

# ---- the scheduled mode ------------------------------------------------------------
# The image's default command, as the README's compose example runs it: s6
# starts, the folders are checked and the crontab is built from
# WPBACKUP_SCHEDULE (here every minute) plus the mounted crontab, as PUID:PGID
# (33:33), and Supercronic runs as PUID:PGID. Within
# three minutes it must have written a new Archive of this site, owned by 33,
# that verifies, and the job's own output must be in the container log.
# Every Archive under /backups, flat or in subfolders (owner decision 86).
archives_of_site() { in_backup find /backups -name "wpbak-$DB_NAME-*.tar.gz" 2>/dev/null || true; }
scheduler_log() { dc --profile schedule logs --no-color scheduler 2>&1; }
all_archives() { in_backup sh -c 'find /backups -name "*.tar.gz" | wc -l'; }
# Another site's Archive in the same directory: no prune may remove it.
OTHER_SITE=/tmp/wpbackup/other-site
step S-other-site-backup 0 in_backup sh -c '
    set -eu
    mkdir -p "$1"
    cat >"$1/wp-config.php"
    echo "<?php" >"$1/index.php"
    wpbackup2 --log-format json backup --wp-dir "$1" --mode files' sh "$OTHER_SITE" <<'PHP'
<?php
define('DB_NAME', 'othersite');
define('DB_USER', 'u');
define('DB_PASSWORD', 'p');
define('DB_HOST', 'db');
$table_prefix = 'wp_';
PHP
OTHER_ARCHIVE=$(tail -1 "$LOGS/S-other-site-backup.out")
log "another site's Archive: ${OTHER_ARCHIVE:-none}"
# prune, one-shot: a rule is required (the image's WPBACKUP_KEEP_* feed only
# the schedule); a Dry Run lists and deletes nothing.
step P-prune-needs-a-rule 2 wb prune
COUNT_BEFORE=$(all_archives)
step P-prune-dryrun 0 wb --whatIf prune --keep 1
step P-prune-dryrun-daily 0 wb --whatIf prune --keep-daily 1
count_is() { [[ $(all_archives) == "$1" ]]; }
not_printed() { ! printed "$1" "$2"; }
check P:needs-a-rule-says-so grep -qF 'at least one retention rule' "$LOGS/P-prune-needs-a-rule.err"
check P:dryrun-deleted-nothing count_is "$COUNT_BEFORE"
check P:dryrun-lists-would-prune printed P-prune-dryrun "would prune "
check P:dryrun-spares-the-other-site not_printed P-prune-dryrun "$OTHER_ARCHIVE"
check P:dryrun-daily-says-why printed P-prune-dryrun-daily "daily: not the newest of its day"
check P:dryrun-daily-keeps-one printed P-prune-dryrun-daily "; kept 1 (--keep-daily 1)"
# A one-shot backup into another subfolder layout: the scheduled prune must
# find it there, delete it, and remove the folders it empties.
step S-backup-old-layout 0 wb backup --archive-subdir 'old/{site}'
OLD_LAYOUT=$(head -1 "$LOGS/S-backup-old-layout.out")
check S:old-layout-is-nested grep -qE "^/backups/old/$DB_NAME/wpbak-$DB_NAME-[0-9]{8}T[0-9]{6}Z\.tar\.gz\$" "$LOGS/S-backup-old-layout.out"
BEFORE=$(archives_of_site)
step S-scheduler-up 0 dc --profile schedule up -d scheduler
SCHEDULED=
for _ in $(seq 1 36); do
    SCHEDULED=$(comm -13 <(sort <<<"$BEFORE") <(archives_of_site | sort) | head -1)
    # The backup prints the Archive's path when it is complete.
    # Captured first: under pipefail, `scheduler_log | grep -q` is
    # false when grep exits before `docker compose logs` has written it all.
    [[ -n $SCHEDULED ]] && grep -qF "$SCHEDULED" <<<"$(scheduler_log)" && break
    sleep 5
done
scheduler_log >"$LOGS/S-scheduler.log" || true
log "scheduled Archive: ${SCHEDULED:-none}"
# The job prunes after its backup: wait for its summary record.
for _ in $(seq 1 24); do
    grep -qF '"event": "prune_completed"' <<<"$(scheduler_log)" && break
    sleep 5
done
scheduler_log >"$LOGS/S-scheduler.log" || true
# This site's Archives (every name) are down to the day's newest; the other
# site's survives, and so does the scheduled one, the newest.
check S:prune-ran-after-the-backup grep -qF '"event": "archive_pruned"' "$LOGS/S-scheduler.log"
check S:pruned-says-why grep -qF 'daily: not the newest of its day' "$LOGS/S-scheduler.log"
check S:pruned-to-one-daily-plus-the-other-site count_is 2
check S:other-sites-archive-survives in_backup test -f "${OTHER_ARCHIVE:-/nonexistent}"
check S:newest-archive-survives in_backup test -f "${SCHEDULED:-/nonexistent}"
supercronic_uid() { [[ $(dc --profile schedule exec -T scheduler ps -o uid= -C supercronic | tr -d ' ') == 33 ]]; }
check S:schedule-lists-the-env-job grep -qF "* * * * * wpbackup2 backup && wpbackup2 prune --keep-daily $SCHEDULED_KEEP_DAILY" "$LOGS/S-scheduler.log"
check S:schedule-merges-the-mounted-crontab grep -qF '0 0 1 1 * wpbackup2 doctor' "$LOGS/S-scheduler.log"
check S:scheduled-backup-wrote-an-archive test -n "$SCHEDULED"
check S:job-output-in-container-log grep -qF "${SCHEDULED:-no archive}" "$LOGS/S-scheduler.log"
check S:supercronic-runs-as-33 supercronic_uid
step S-scheduler-stop 0 dc --profile schedule stop -t 30 scheduler
# The schedule fires every minute: a later job may have run before the stop,
# and its prune (one daily) removed the earlier scheduled Archive. The checks
# below are of the newest one that survives (by name: a UTC timestamp).
if [[ -n $SCHEDULED ]]; then
    NEWEST=$(archives_of_site | awk -F/ '{print $NF "\t" $0}' | sort | tail -1 | cut -f2)
    if [[ -n $NEWEST && $NEWEST != "$SCHEDULED" ]]; then
        log "a later scheduled job ran before the stop; newest Archive: $NEWEST"
        SCHEDULED=$NEWEST
    fi
fi
if [[ -n $SCHEDULED ]]; then
    check S:scheduled-archive-owned-by-33 owned_by_33 "$SCHEDULED"
    step S-verify-scheduled 0 wb verify --archive "$SCHEDULED"
fi
# L3IO_WP_BACKUP_ARCHIVE_SUBDIR in the scheduler's environment: the Archive is
# under /backups/<site>/<yyyy>/<mm>, its folders made 0700 and owned by 33;
# the prune reached the flat Archives and the old layout's, and removed the
# old layout's folders; `latest`, from the flat-configured one-shot service,
# finds the nested Archive.
scheduled_is_nested() { [[ $SCHEDULED =~ ^/backups/$DB_NAME/[0-9]{4}/[0-9]{2}/wpbak-$DB_NAME-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]]; }
folder_is_private_33() { [[ $(in_backup stat -c %a:%u:%g "$1") == 700:33:33 ]]; }
check S:scheduled-archive-in-subfolders scheduled_is_nested
check S:subfolder-0700-owned-by-33 folder_is_private_33 "$(dirname "${SCHEDULED:-/nonexistent/x}")"
check S:site-folder-0700-owned-by-33 folder_is_private_33 "/backups/$DB_NAME"
check S:old-layout-archive-pruned absent_in_backups "old/$DB_NAME/$(basename "${OLD_LAYOUT:-none}")"
check S:old-layout-folders-removed absent_in_backups old
check S:folder-removal-logged grep -qF "\"path\": \"/backups/old/$DB_NAME\"" "$LOGS/S-scheduler.log"
check S:folder-removal-event grep -qF '"event": "archive_dir_removed"' "$LOGS/S-scheduler.log"
step S-latest-nested 0 wb --whatIf restore --archive latest --mode files
check S:latest-is-the-nested-scheduled-archive printed S-latest-nested "archive latest: ${SCHEDULED:-none}"
step S-prune-nested-dryrun 0 wb --whatIf prune --keep 1
check S:prune-keeps-the-nested-newest printed S-prune-nested-dryrun "kept ${SCHEDULED:-none} "

# ---- the scheduled mode as a non-root user (owner decision 87) ----------------------
# `user: "33:33"`, read-only root, no s6: the entrypoint checks the folders,
# builds the crontab under the TMPDIR tmpfs and execs Supercronic as 33:33. It
# must write a new Archive, which verifies, owned by 33.
user_scheduler_log() { dc --profile schedule logs --no-color scheduler-user 2>&1; }
BEFORE=$(archives_of_site)
step U-scheduler-user-up 0 dc --profile schedule up -d scheduler-user
USER_SCHEDULED=
for _ in $(seq 1 36); do
    USER_SCHEDULED=$(comm -13 <(sort <<<"$BEFORE") <(archives_of_site | sort) | head -1)
    [[ -n $USER_SCHEDULED ]] && grep -qF "$USER_SCHEDULED" <<<"$(user_scheduler_log)" && break
    sleep 5
done
user_scheduler_log >"$LOGS/U-scheduler-user.log" || true
dc --profile schedule exec -T scheduler-user ps -eo uid=,comm= >"$LOGS/U-processes.txt" 2>&1 || true
log "user-scheduled Archive: ${USER_SCHEDULED:-none}"
supercronic_uid_user() { [[ $(dc --profile schedule exec -T scheduler-user ps -o uid= -C supercronic | tr -d ' ') == 33 ]]; }
no_s6() { ! grep -qE 's6-svscan|s6-supervise' "$LOGS/U-processes.txt"; }
check U:user-scheduled-backup-wrote-an-archive test -n "$USER_SCHEDULED"
check U:job-output-in-container-log grep -qF "${USER_SCHEDULED:-no archive}" "$LOGS/U-scheduler-user.log"
check U:supercronic-runs-as-33 supercronic_uid_user
check U:no-s6 no_s6
check U:crontab-under-tmpdir grep -qF 'scheduled jobs (as uid 33:33)' "$LOGS/U-scheduler-user.log"
step U-scheduler-user-stop 0 dc --profile schedule stop -t 30 scheduler-user
if [[ -n $USER_SCHEDULED ]]; then
    NEWEST=$(archives_of_site | awk -F/ '{print $NF "\t" $0}' | sort | tail -1 | cut -f2)
    [[ -n $NEWEST ]] && USER_SCHEDULED=$NEWEST
    check U:archive-owned-by-33 owned_by_33 "$USER_SCHEDULED"
    step U-verify-user-scheduled 0 wb verify --archive "$USER_SCHEDULED"
fi

# ---- another PUID against the shared volumes (owner decision 87) --------------------
# The incident this replaces: a one-shot with PUID=12345 re-owned the shared
# state volume, and the next normal Run failed. Now a one-shot and a scheduled
# start as another PUID are refused with the fix, the state and Archives keep
# every owner and mode, and the normal Run afterwards works.
volumes_tree() { in_backup sh -c 'find /var/lib/wpbackup/state /backups -printf "%u:%g %m %p\n" | sort'; }
volumes_unchanged() { [[ $(volumes_tree) == "$VOLUMES_BEFORE" ]]; }
volumes_owned_by_33() { [[ -z $(in_backup find /var/lib/wpbackup/state /backups ! -uid 33 -print -quit) ]]; }
VOLUMES_BEFORE=$(volumes_tree)
printf '%s\n' "$VOLUMES_BEFORE" | awk '{print $1, $2}' | sort | uniq -c >"$LOGS/X-volumes-owners-before.txt"
step X-oneshot-other-puid 3 dc --profile tools run --rm -T -e PUID=12345 -e PGID=12345 oneshot backup
check X:oneshot-says-never-changes-ownership grep -qF 'wpbackup2 never changes ownership' "$LOGS/X-oneshot-other-puid.err"
check X:oneshot-names-the-state-folder grep -qF "/var/lib/wpbackup/state" "$LOGS/X-oneshot-other-puid.err"
check X:oneshot-names-the-uid grep -qF "by uid 12345:12345" "$LOGS/X-oneshot-other-puid.err"
check X:oneshot-left-volumes-unchanged volumes_unchanged
step X-scheduled-other-puid '*' timeout 120 docker compose -f "$HERE/compose.yaml" -p "$PROJECT" --profile "$ENGINE" \
    --profile tools run --rm -T -e PUID=12345 -e PGID=12345 oneshot
check X:scheduled-other-puid-stopped test "$LAST_RC" != 0
check X:scheduled-says-not-started grep -qF 'the scheduled mode was not started' "$LOGS/X-scheduled-other-puid.out" "$LOGS/X-scheduled-other-puid.err"
check X:scheduled-left-volumes-unchanged volumes_unchanged
step X-backup-after 0 wb backup
step X-verify-after 0 wb verify --archive "$(head -1 "$LOGS/X-backup-after.out")"
check X:volumes-still-33 volumes_owned_by_33

# ---- report -----------------------------------------------------------------------
{
    echo "engine=$ENGINE client=$L3IO_DB_CLIENT"
    printf '%s\n' "${RESULTS[@]}"
} | tee "$LOGS/summary.txt"
[[ $FAILED == 0 ]]
