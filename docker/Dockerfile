# syntax=docker/dockerfile:1
#
# The official l3io-wordpress-backup image (owner decision 77): `wpbackup2` as a
# sidecar next to the official `wordpress` image. Built by
# .github/workflows/docker.yml from the PyPI release named by VERSION, never
# from the working tree.
#
#   docker buildx build -f docker/Dockerfile --build-arg VERSION=1.0.0 docker
#
# Build arguments:
#   VERSION     required; the l3io-wordpress-backup release installed (and
#               asserted after the install).
#   DB_CLIENT   mysql (default) or mariadb: the dump/load client family. Ubuntu's
#               mysql-client and mariadb-client conflict, so one image carries
#               one; docs/docker.md records which servers each was proven against.
#   BASE_IMAGE  the base, pinned by digest: linuxserver's public Ubuntu base (s6
#               overlay, PUID/PGID through the `abc` user). An internal build from
#               the family's l3io base is this one line.
#   REVISION    the source commit, for the OCI label.
#
# The `wheels` build context is for tests/sidecar/run.sh ONLY: given
# (`--build-context wheels=DIR`), the wheels in DIR are installed instead of the
# PyPI release, and VERSION must equal the wheel's version. The release workflow
# never passes it; tests/contracts/test_docker_image.py asserts that.
#
# Writable at run time: $XDG_STATE_HOME (/var/lib/wpbackup/state: the Site Lock,
# the Run Journal, Safety Snapshots; a volume), $TMPDIR (/tmp/wpbackup: the Run's
# workspace and the plaintext Dump; a tmpfs), /backups (the Archives; a volume).
#
# No HEALTHCHECK: a one-shot run has nothing to probe, and in the scheduled mode
# s6 restarts Supercronic if it dies; a job's failure is in the container log
# (and in its exit status there), not a container health state.

ARG BASE_IMAGE=ghcr.io/linuxserver/baseimage-ubuntu:resolute-fe408847-ls32@sha256:2607002007f92888e46e60c981fbdf315a39e56018bb9ce651dd72ec1376c7a0
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754
ARG GO_IMAGE=golang:1.27.4@sha256:08bcb99a38f85a265f9a93d1d3323e2440eec333380f3ea26ee0dec9264bb41c

FROM ${UV_IMAGE} AS uv

# Empty unless a test passes --build-context wheels=DIR.
FROM scratch AS wheels

# ---- the virtual environment -------------------------------------------------
FROM ${BASE_IMAGE} AS venv
ARG VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never VIRTUAL_ENV=
# Wheels only (--no-build): every dependency, the family included (owner
# decision 76), installs from PyPI as published.
RUN --mount=type=bind,from=wheels,target=/wheels set -eu; \
    test -n "${VERSION:-}" || { echo "--build-arg VERSION=<release> is required" >&2; exit 2; }; \
    uv venv -q --python /usr/bin/python3 /opt/wpbackup; \
    if ls /wheels/*.whl >/dev/null 2>&1; then \
      echo "TEST BUILD: installing $(cd /wheels && echo *.whl), not the PyPI release"; \
      uv pip install --python /opt/wpbackup --no-build /wheels/*.whl; \
    else \
      uv pip install --python /opt/wpbackup --no-build "l3io-wordpress-backup==${VERSION}"; \
    fi; \
    /opt/wpbackup/bin/python -c "import importlib.metadata as m, sys; v = m.version('l3io-wordpress-backup'); print('l3io-wordpress-backup', v, '/ l3io-wordpress-database', m.version('l3io-wordpress-database')); sys.exit(0 if v == sys.argv[1] else f'installed {v}, VERSION says {sys.argv[1]}')" "$VERSION"; \
    /opt/wpbackup/bin/wpbackup2 --version

# ---- age: v1.3.2, built from its module with a current Go ---------------------
# The release binaries of age 1.3.2 are built with Go 1.27.0, whose standard
# library has four fixed HIGH advisories (CVE-2026-78667, -78669, -97031,
# -111943; the scan policy in docker.yml fails on fixable HIGH). The same tagged
# source, built with Go 1.27.4, has none. The module is fetched through the Go
# checksum database and its hash is asserted below, so the source is as pinned
# as a checksummed download. Cross-compiled on the build platform (no QEMU).
# The age CLI is the passphrase backend's fallback only: with the
# l3io-age-passphrase binder importable (it is, from the venv), `auto` uses the
# binder.
FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS age
ARG TARGETARCH
ENV CGO_ENABLED=0 GOFLAGS=-trimpath GOTOOLCHAIN=local
RUN set -eu; \
    case "${TARGETARCH:?}" in amd64|arm64) ;; *) echo "unsupported TARGETARCH $TARGETARCH" >&2; exit 2 ;; esac; \
    # A cross-compiled `go install` lands in bin/linux_<arch>/, a native one in bin/.
    GOARCH=$TARGETARCH go install filippo.io/age/cmd/age@v1.3.2 filippo.io/age/cmd/age-plugin-batchpass@v1.3.2; \
    mkdir -p /out; \
    for b in age age-plugin-batchpass; do \
      f="$(go env GOPATH)/bin/$b"; \
      [ -f "$(go env GOPATH)/bin/linux_$TARGETARCH/$b" ] && f="$(go env GOPATH)/bin/linux_$TARGETARCH/$b"; \
      mv "$f" /out/; \
      go version -m "/out/$b" | grep -qF 'filippo.io/age	v1.3.2	h1:r6RSZLFSMm6rzKepZ7ZAYkKCu14f3/Me8c7uKYh7C8c=' \
        || { echo "$b: not built from filippo.io/age v1.3.2 with the pinned module hash" >&2; go version -m "/out/$b" >&2; exit 1; }; \
    done

# ---- Supercronic: pinned by version, checked by SHA-256 ------------------------
# Both architectures are fetched (and checked by BuildKit before use);
# TARGETARCH picks one.
FROM ${BASE_IMAGE} AS supercronic
ARG TARGETARCH
ADD --checksum=sha256:cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 \
    https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-amd64 /dl/supercronic-amd64
ADD --checksum=sha256:4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 \
    https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-arm64 /dl/supercronic-arm64
RUN set -eu; \
    case "${TARGETARCH:?}" in amd64|arm64) ;; *) echo "unsupported TARGETARCH $TARGETARCH" >&2; exit 2 ;; esac; \
    install -D -m 0755 "/dl/supercronic-$TARGETARCH" /out/supercronic

# ---- runtime ----------------------------------------------------------------
FROM ${BASE_IMAGE}
ARG VERSION
ARG REVISION=unknown
ARG DB_CLIENT=mysql
LABEL org.opencontainers.image.title="l3io-wordpress-backup" \
      org.opencontainers.image.description="wpbackup2: back up and restore a WordPress site; a sidecar next to the official wordpress image" \
      org.opencontainers.image.source="https://github.com/LiquidLogicLabs/l3io-wordpress-backup" \
      org.opencontainers.image.url="https://github.com/LiquidLogicLabs/l3io-wordpress-backup" \
      org.opencontainers.image.documentation="https://github.com/LiquidLogicLabs/l3io-wordpress-backup#run-as-a-docker-sidecar" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="LiquidLogicLabs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      io.l3io.wp-backup.db-client="${DB_CLIENT}"

RUN set -eu; \
    case "$DB_CLIENT" in \
      mysql)   client=mysql-client ;; \
      mariadb) client=mariadb-client ;; \
      *) echo "DB_CLIENT must be mysql or mariadb" >&2; exit 2 ;; \
    esac; \
    apt-get update; \
    # The base's own packages too: a fix published after the base was built
    # (the scan fails on a fixable HIGH) is taken here, not left for the base.
    apt-get upgrade -y; \
    apt-get install -y --no-install-recommends python3 ca-certificates "$client"; \
    rm -rf /var/lib/apt/lists/*

COPY --from=venv /opt/wpbackup /opt/wpbackup
COPY --from=age /out/ /usr/local/bin/
COPY --from=supercronic /out/ /usr/local/bin/
COPY root/ /

# The sidecar's settings (owner decision 60): the Site Root and the Archive
# directory, so a backup is `wpbackup2 backup` and a restore
# `wpbackup2 restore --archive latest`, with no flags. PUID/PGID: the base's
# `abc` user, moved to www-data's ids by default. WPBACKUP_SCHEDULE: the
# scheduled mode's daily backup, and WPBACKUP_KEEP the prune after each
# successful one (docker/root/usr/local/bin/l3io-wpbackup-crontab).
ENV PATH=/opt/wpbackup/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    VIRTUAL_ENV=/opt/wpbackup \
    HOME=/home/wpbackup \
    XDG_STATE_HOME=/var/lib/wpbackup/state \
    TMPDIR=/tmp/wpbackup \
    L3IO_AGE_PASSPHRASE_BACKEND=auto \
    L3IO_WP_BACKUP_WP_DIR=/var/www/html \
    L3IO_WP_BACKUP_ARCHIVE_DIR=/backups \
    PUID=33 \
    PGID=33 \
    WPBACKUP_SCHEDULE="0 3 * * *" \
    WPBACKUP_KEEP=14 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2

# The uid/gid that owns the WordPress files: www-data (33) in the official
# image. These are the build-time owners; PUID/PGID move them at start.
RUN set -eu; \
    mkdir -p /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup /etc/l3io-wp-backup; \
    chown 33:33 /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup; \
    chmod 0700 /home/wpbackup /var/lib/wpbackup/state /backups /tmp/wpbackup; \
    chmod 0755 /usr/local/bin/l3io-wpbackup-entrypoint /usr/local/bin/l3io-wpbackup-crontab \
      /etc/s6-overlay/s6-rc.d/init-wpbackup-config/run /etc/s6-overlay/s6-rc.d/svc-wpbackup-schedule/run; \
    wpbackup2 --version; age --version; supercronic -version; \
    (mysqldump --version || mariadb-dump --version)

WORKDIR /home/wpbackup
ENTRYPOINT ["/usr/local/bin/l3io-wpbackup-entrypoint"]
CMD []
