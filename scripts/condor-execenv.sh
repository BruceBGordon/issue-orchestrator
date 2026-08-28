#!/bin/sh
# Host-side driver for the containerized execution environment
# (issue #7115, docs/architecture/execenv/). Entirely opt-in: nothing
# in the orchestrator or the gates touches this unless you run it.
#
#   scripts/condor-execenv.sh build      build the image
#   scripts/condor-execenv.sh up         start the pool container
#   scripts/condor-execenv.sh selftest   run the in-container proofs
#   scripts/condor-execenv.sh down       stop and remove the container
#
# The repo this script lives in is mounted read-only at /repo; the
# container builds its own Linux venv in the io-work volume.
set -eu

IMAGE="${IO_EXECENV_IMAGE:-io-execenv:latest}"
CONTAINER="${IO_EXECENV_CONTAINER:-io-execenv}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# linux/amd64 everywhere: htcondor.org ships no arm64 debs, and the
# current LTS (with working cgroup-v2 family tracking) only exists
# there. Native on CI runners; Rosetta/qemu-emulated on Apple Silicon —
# the same posture as the macOS pool's x86_64 tarball.
PLATFORM="linux/amd64"

case "${1:-}" in
build)
    exec docker build --platform "$PLATFORM" -t "$IMAGE" "$REPO_ROOT/docker/execenv"
    ;;
up)
    docker volume create io-work >/dev/null
    docker volume create io-spool >/dev/null
    # --stop-timeout 60: docker stop's default 10s SIGTERM-to-SIGKILL
    # defeats condor_master's clean drain (ADR-0003; #7115 finding 8).
    # --cgroupns private + cgroup v2 delegation is what the entrypoint
    # verifies before starting anything.
    # CAP_SYS_ADMIN: solely so the entrypoint can remount the
    # read-only cgroup fs and delegate controllers (Docker Desktop
    # mounts it ro even with a private cgroup namespace). Not
    # --privileged - ADR-0013's posture, minimally.
    exec docker run -d --name "$CONTAINER" \
        --platform "$PLATFORM" \
        --stop-timeout 60 \
        --cgroupns private \
        --cap-add SYS_ADMIN \
        -v io-work:/work \
        -v io-spool:/var/lib/condor \
        -v "$REPO_ROOT":/repo:ro \
        "$IMAGE"
    ;;
selftest)
    exec docker exec --user io "$CONTAINER" /usr/local/bin/execenv-selftest
    ;;
down)
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm "$CONTAINER" >/dev/null 2>&1 || true
    echo "execenv: container removed (volumes io-work/io-spool kept)"
    ;;
*)
    echo "usage: $0 {build|up|selftest|down}" >&2
    exit 64
    ;;
esac
