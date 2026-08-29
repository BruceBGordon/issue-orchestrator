# The io execution environment: HTCondor on Linux, in one container.
# Implements the validation-lane increment of the design record at
# docs/architecture/execenv/ (issue #7115). Agent sessions and io-under-
# condor_master supervision are deliberately NOT here yet: they gate on
# ADR-0016 (credential provisioning), which is unresolved design.
#
# Pinned by digest per ADR-0007, and — after empirical defeat — per
# ADR-0007's htcondor.org LTS preference too: Ubuntu 24.04's own
# htcondor is 23.4.0, whose starter silently declines cgroup-v2 family
# tracking (probe says "/htcondor is useable", then Create_Process
# spawns with no tracking; condor_rm kills only the direct child and a
# setsid grandchild survives — reproduced live in this container,
# 2026-08-28). Containment is this environment's reason to exist, so
# the current LTS from htcondor.org it is. That repo ships amd64 only,
# so the image is built and run linux/amd64 (native on CI; emulated on
# Apple Silicon, matching the macOS pool's own Rosetta posture).
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

# ADR-0007's reproducible version contract: the exact LTS point release
# is a build argument, so today's build and next month's build resolve
# identically unless someone deliberately edits the pin.
ARG CONDOR_PACKAGE_VERSION=24.0.23-1+ubu24

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        git \
        ca-certificates \
        curl \
        gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://research.cs.wisc.edu/htcondor/repo/keys/HTCondor-24.0-Key \
        | gpg --dearmor -o /etc/apt/keyrings/htcondor.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/htcondor.gpg] https://research.cs.wisc.edu/htcondor/repo/ubuntu/24.0 noble main" \
        > /etc/apt/sources.list.d/htcondor.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "htcondor=${CONDOR_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# uv builds the repo's Linux venv inside the container (the mounted
# repo's own .venv is a macOS artifact and must not be touched).
# Digest-pinned like the base image: a movable tag is not a pin.
COPY --from=ghcr.io/astral-sh/uv:0.9.27@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a /uv /usr/local/bin/uv

# Single trusted user, matching the personal-pool posture (ADR-0009/0013:
# the job privilege boundary is not a security boundary). Ubuntu 24.04
# ships an "ubuntu" user at uid 1000; repurpose the uid for a clean name.
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd --create-home --uid 1000 io

COPY condor/00-io-execenv.config /etc/condor/config.d/00-io-execenv.config
COPY entrypoint.sh /usr/local/bin/execenv-entrypoint
COPY selftest.sh /usr/local/bin/execenv-selftest
RUN chmod 0755 /usr/local/bin/execenv-entrypoint /usr/local/bin/execenv-selftest \
    && mkdir -p /work && chown io:io /work

# PER_JOB_HISTORY_DIR is written by the schedd, which runs as the condor
# user here; condor never creates the directory and disables per-job
# accounting silently when it is missing (#7127). The path is derived
# from the installed configuration, not guessed.
RUN set -eu; \
    spool="$(condor_config_val SPOOL)"; \
    install -d -m 0755 "$spool/per-job-history"; \
    chown --reference="$spool" "$spool/per-job-history"

# condor_master is PID 1 (ADR-0003): it is already a supervisor, and
# docker stop's SIGTERM drains the pool cleanly — provided the run
# recipe passes --stop-timeout 60 (default 10s defeats the drain).
ENTRYPOINT ["/usr/local/bin/execenv-entrypoint"]
