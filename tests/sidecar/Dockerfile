# syntax=docker/dockerfile:1
#
# LOCAL TEST IMAGE ONLY -- l3io-wordpress-backup:sidecar-test-<client>.
# Not a published image. Built by tests/sidecar/run.sh from a build context it
# assembles in a scratch directory: this repository's wheel (`uv build
# --no-sources`) and pyproject.toml, nothing else from the tree.
#
# One image per client family (DB_CLIENT=mysql|mariadb): Ubuntu's mysql-client
# and mariadb-client declare Conflicts with each other, so a single image cannot
# carry both from the distribution's packages.
#
# LOCAL_DEPS=1 (run.sh, L3IO_LOCAL_WHEELS set): the wheels in the context's
# local-deps/ replace the published ones they name, after the index install.
# For a run that needs a family release not yet on PyPI (for example
# l3io-wordpress-database built from its checkout); never for a release.

ARG UBUNTU_IMAGE=ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754

FROM ${UV_IMAGE} AS uv

# ---- build: the venv, from the wheel ---------------------------------------
FROM ${UBUNTU_IMAGE} AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /ctx
ARG LOCAL_DEPS=0
COPY pyproject.toml ./
COPY *.whl ./
COPY local-deps/ ./local-deps/
# The repository's own clean-install recipe (ci.yml "every optional extra
# installs, wheels only"), without extras: the dependencies are read from
# pyproject.toml and resolve from PyPI alone; the family is a base dependency
# (owner decision 76), so no extra is needed. Then the built wheel, --no-deps.
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
RUN uv venv -q --python /usr/bin/python3 /opt/wpbackup \
 && uv pip install --python /opt/wpbackup --no-build -r pyproject.toml \
 && if [ "$LOCAL_DEPS" = 1 ]; then \
      uv pip install --python /opt/wpbackup --no-deps --reinstall ./local-deps/*.whl; \
    fi \
 && uv pip install --python /opt/wpbackup --no-deps ./*.whl \
 && /opt/wpbackup/bin/wpbackup2 --version \
 && /opt/wpbackup/bin/python -c "import importlib.metadata as m; print('l3io-wordpress-database', m.version('l3io-wordpress-database'))"

# age 1.3.2 and age-plugin-batchpass, pinned and checksummed as ci.yml pins them.
ADD --checksum=sha256:cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 \
    https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz \
    /tmp/age.tar.gz
RUN mkdir /tmp/age && tar -xzf /tmp/age.tar.gz -C /tmp/age --strip-components=1

# ---- runtime ----------------------------------------------------------------
FROM ${UBUNTU_IMAGE}
ARG DB_CLIENT=mysql
# The uid/gid that owns the WordPress files. The official image's www-data is 33.
ARG RUN_UID=33
ARG RUN_GID=33
LABEL org.opencontainers.image.title="l3io-wordpress-backup sidecar (local test only)"

RUN set -eu; \
    case "$DB_CLIENT" in \
      mysql)   client=mysql-client ;; \
      mariadb) client=mariadb-client ;; \
      *) echo "DB_CLIENT must be mysql or mariadb" >&2; exit 2 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 ca-certificates "$client"; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/wpbackup /opt/wpbackup
COPY --from=build /tmp/age/age /tmp/age/age-plugin-batchpass /usr/local/bin/

# Where the tool keeps state (docs/logging.md "The Site Lock", docs/restore.md
# "Per-user state directory"): the Site Lock, the Run Journal and the default
# Safety Snapshots, under $XDG_STATE_HOME, owner-only, outside the Site Root.
# The Run workspace (the plaintext Dump) follows $TMPDIR, which compose mounts
# as a tmpfs so the Dump never touches disk (docs/api.md, TMPDIR).
RUN set -eu; \
    mkdir -p /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup; \
    chown "$RUN_UID:$RUN_GID" /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup; \
    chmod 0700 /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup

# The sidecar's settings (owner decision 60): the Site Root and the Archive
# directory, so a scheduled backup is `wpbackup2 backup` and a restore
# `wpbackup2 restore --archive latest`, with no flags.
ENV PATH=/opt/wpbackup/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/home/wpbackup \
    XDG_STATE_HOME=/var/lib/wpbackup/state \
    TMPDIR=/tmp/wpbackup \
    L3IO_AGE_PASSPHRASE_BACKEND=auto \
    L3IO_WP_BACKUP_WP_DIR=/var/www/html \
    L3IO_WP_BACKUP_ARCHIVE_DIR=/backups

# The web server's user by default, so state, Archives and restored files are
# its own. A restore started as root (`docker compose exec -u 0`) switches to
# the Site Root's owner itself (owner decision 58); run.sh exercises both.
USER ${RUN_UID}:${RUN_GID}
WORKDIR /home/wpbackup
# A sidecar idles; jobs are `docker compose exec backup wpbackup2 ...`.
# compose sets `init: true`, so signals reach this and the jobs.
CMD ["sleep", "infinity"]
