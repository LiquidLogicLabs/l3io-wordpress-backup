#!/usr/bin/env bash
# LOCAL smoke run of wpbackup2 as a Docker sidecar beside the official
# WordPress image. Not part of the pytest suite and not run by CI.
#
#   tests/sidecar/run.sh mysql      # mysql:8.0,    mysql-client 8.4 sidecar
#   tests/sidecar/run.sh mariadb    # mariadb:11.4, mariadb-client 11.8 sidecar
#
# Environment:
#   L3IO_DB_CLIENT=mysql|mariadb  sidecar client family (default: the engine's own)
#   REMOVE_IMAGE=1                remove the sidecar image on exit
#   SKIP_BUILD=1                  reuse an existing sidecar image
#   KEEP_STACK=1                  leave the stack and scratch dir up (debugging;
#                                 tear down with the `docker compose ... down -v` it prints)
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

usage() { echo "usage: $0 mysql|mariadb" >&2; exit 2; }
[[ $# -eq 1 ]] || usage
ENGINE=$1
[[ $ENGINE == mysql || $ENGINE == mariadb ]] || usage

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
L3IO_DB_CLIENT=${L3IO_DB_CLIENT:-$ENGINE}
IMAGE="l3io-wordpress-backup:sidecar-test-$L3IO_DB_CLIENT"

rand() { openssl rand -hex "$1"; }
L3IO_SCRATCH=$(mktemp -d -t l3io-sidecar.XXXXXX)
LOGS=$L3IO_SCRATCH/logs
mkdir -p "$LOGS" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/tls"
chmod 0755 "$L3IO_SCRATCH" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/tls"
PROJECT="l3io-sidecar-$ENGINE-$(rand 4)"
export L3IO_SCRATCH L3IO_DB_CLIENT

log() { printf '[%(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "$LOGS/run.log" >&2; }
dc() { docker compose -f "$HERE/compose.yaml" -p "$PROJECT" --profile "$ENGINE" "$@"; }

# ---- teardown ----------------------------------------------------------------
SECRETS=()
cleanup() {
    local rc=$?
    set +e
    log "teardown (exit $rc)"
    dc --profile tools logs --no-color --timestamps >"$LOGS/compose.log" 2>&1
    # Leak check: no generated secret may appear in any collected log.
    local leaks=0 s
    for s in "${SECRETS[@]}"; do
        grep -rqF -- "$s" "$LOGS" && leaks=$((leaks + 1))
    done
    log "secret leak check over $LOGS: $leaks of ${#SECRETS[@]} secrets found"
    if [[ ${KEEP_STACK:-0} == 1 ]]; then
        log "KEEP_STACK=1: left up; tear down with: L3IO_SCRATCH=$L3IO_SCRATCH L3IO_DB_CLIENT=$L3IO_DB_CLIENT docker compose -f $HERE/compose.yaml -p $PROJECT --profile $ENGINE --profile tools down -v"
        exit "$rc"
    fi
    dc --profile tools down -v --remove-orphans -t 5 >>"$LOGS/teardown.log" 2>&1
    # The Site Root is owned by www-data (33) inside the containers.
    if [[ -d $L3IO_SCRATCH/html ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        docker run --rm --user 0 -v "$L3IO_SCRATCH:/s" --entrypoint rm "$IMAGE" -rf /s/html
    fi
    rm -rf "$L3IO_SCRATCH/html" "$L3IO_SCRATCH/tls" "$L3IO_SCRATCH/content" "$L3IO_SCRATCH/ctx" \
        "$L3IO_SCRATCH"/*.env "$L3IO_SCRATCH/wp-config.docker.php"
    [[ ${REMOVE_IMAGE:-0} == 1 ]] && docker image rm "$IMAGE" >/dev/null 2>&1
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
cat >"$L3IO_SCRATCH/backup.env" <<EOF
L3IO_WP_BACKUP_ADMIN_USER=root
L3IO_WP_BACKUP_ADMIN_PASSWORD=$DB_ROOT_PW
EOF

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
    if [[ $want != '*' && $rc != "$want" ]]; then verdict=MISMATCH; FAILED=1; fi
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
log "engine=$ENGINE client=$L3IO_DB_CLIENT project=$PROJECT scratch=$L3IO_SCRATCH"
log "repo HEAD $(git -C "$REPO" rev-parse --short HEAD); modified tracked files: $(git -C "$REPO" status --porcelain --untracked-files=no | wc -l)"
if [[ ${SKIP_BUILD:-0} != 1 ]]; then
    mkdir -p "$L3IO_SCRATCH/ctx"
    step build-wheel 0 bash -c "cd '$REPO' && uv build --no-sources --wheel -o '$L3IO_SCRATCH/ctx'"
    cp "$REPO/pyproject.toml" "$L3IO_SCRATCH/ctx/"
    step build-image 0 docker build -f "$HERE/Dockerfile" --build-arg "DB_CLIENT=$L3IO_DB_CLIENT" \
        -t "$IMAGE" "$L3IO_SCRATCH/ctx"
    [[ $FAILED == 0 ]] || { printf '%s\n' "${RESULTS[@]}"; exit 1; }
fi

# ---- stack -----------------------------------------------------------------------
step stack-up 0 dc up -d --wait --wait-timeout 300
[[ $FAILED == 0 ]] || { printf '%s\n' "${RESULTS[@]}"; exit 1; }
in_backup sh -c 'id; wpbackup2 --version; age --version; (mysqldump --version || mariadb-dump --version) 2>&1; ls -ld /var/www/html /backups "$XDG_STATE_HOME" "$TMPDIR"' \
    >"$LOGS/sidecar-env.txt" 2>&1 || true
step passphrase-passthrough 0 env L3IO_WP_BACKUP_PASSPHRASE="$PASSPHRASE" \
    docker compose -f "$HERE/compose.yaml" -p "$PROJECT" --profile "$ENGINE" \
    exec -T -e L3IO_WP_BACKUP_PASSPHRASE backup sh -c 'test -n "$L3IO_WP_BACKUP_PASSPHRASE"'

# ---- WordPress install and content ---------------------------------------------
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
check A:default-target-in-archive-dir printed A-backup "/backups/wpbackup2-$DB_NAME-"
step A-verify 0 wb verify --archive "$(head -1 "$LOGS/A-backup.out")"
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

# The administrator: backup.env sets both L3IO_WP_BACKUP_ADMIN_USER and _PASSWORD,
# the environment pair README's credential table documents. No TLS is required
# (owner decision 56); the database's own certificate is exercised by the
# opt-in steps. --force: the database half of owner decision 57 (no flag over
# a WordPress database) arrives with the direct load; the files half needs none.
RESTORE=(restore --archive latest --force)
db_sql() {
    local client=mysql
    [[ $ENGINE == mariadb ]] && client=mariadb
    dc exec -T "db-$ENGINE" sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" $client -uroot" <<<"$1"
}
db_sql "SHOW GRANTS FOR '$DB_USER'@'%';" >"$LOGS/B-wordpress-account-grants.txt" 2>&1 || true
step B-restore-dryrun 0 wb_pp --whatIf "${RESTORE[@]}"
check B:dryrun-names-latest printed B-restore-dryrun "archive latest: /backups/enc.tar.gz"
check B:latest-logged logged B-restore-dryrun archive_latest_selected
check B:dryrun-changed-nothing absent_in_site "wp-content/uploads/$UPLOAD_REL"
step B-restore-dryrun-tls-verified 0 wb_pp --whatIf "${RESTORE[@]}" --db-tls verify-identity --db-tls-ca "$CA"
step B-restore-dryrun-tls-untrusted-ca 3 wb_pp --whatIf "${RESTORE[@]}" --db-tls verify-identity
step B-restore-dryrun-tls-required 0 wb_pp --whatIf "${RESTORE[@]}" --db-tls required
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

# A restore started as root, with no flag but --archive (the Mode from the
# environment): it reads the passphrase and the Archive, then runs as the Site
# Root's owner, www-data (owner decision 58). A WordPress Site Root needs no
# --force (decision 57). The Archive is named: B-restore's staging cutover
# rewrote DB_NAME, so this site's database no longer matches what its
# Archives record and --archive latest rightly finds none (exit 2,
# NoArchiveForSiteError) until the direct load (part 2) removes the cutover.
step B-latest-after-cutover 2 wb_pp --whatIf restore --archive latest --mode files
step B-restore-files-as-root 0 wb_root_pp restore --archive /backups/enc.tar.gz
check root:switched-to-the-owner logged B-restore-files-as-root restore_runs_as_site_owner
check root:files-owned-by-33 no_foreign_owner
check root:page-200-with-sentinel page_serves_sentinel
step B-backup-after-restore 0 wb backup --archive /backups/after.tar.gz

db_sql "SHOW DATABASES;" >"$LOGS/databases-after-restore.txt" 2>&1 || true

# ---- Phase C: a restore onto the getenv_docker() config --------------------------
# A database restore would rewrite the getenv_docker() DB_NAME, so it is refused
# (exit 3, EnvironmentDrivenTargetError) until the direct load (part 2), a Dry
# Run included. --overwrite-wp-config in a files-only Mode carries the target's
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
step C-restore-dryrun-docker-config 3 wb_pp --whatIf "${ENC[@]}" --force
step C-restore-docker-config 3 wb_pp "${ENC[@]}" --force
OVERWRITE=("${ENC[@]}" --mode files --overwrite-wp-config)
step C-overwrite-dryrun-docker-config 0 wb_pp --whatIf "${OVERWRITE[@]}"
check C:overwrite-dryrun-changed-nothing site_config_is_docker
step C-overwrite-docker-config 0 wb_pp "${OVERWRITE[@]}"
db_lines >"$LOGS/C-wp-config-after-overwrite.txt" || true
check C:overwrite-kept-getenv-docker-db db_all_getenv_docker
check C:overwrite-landed-archives-file in_backup grep -qF "\$table_prefix = 'wp_';" "$SITE/wp-config.php"
# PHP itself loads the file (so getenv_docker() is defined) and resolves DB_NAME
# from the environment. Not the sentinel page: Phase B's restore moved the
# restored data to the staging database, and the environment still names the
# original one, where the post was deleted.
check C:overwrite-php-resolves-env-db-name php_db_name_is_env
check C:overwrite-home-200 code_is / 200
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

# ---- report -----------------------------------------------------------------------
{
    echo "engine=$ENGINE client=$L3IO_DB_CLIENT"
    printf '%s\n' "${RESULTS[@]}"
} | tee "$LOGS/summary.txt"
[[ $FAILED == 0 ]]
