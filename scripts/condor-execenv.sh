#!/bin/sh
# Host-side driver for the containerized execution environment
# (issue #7115, docs/architecture/execenv/). Entirely opt-in: nothing
# in the orchestrator or the gates touches this unless you run it.
#
#   scripts/condor-execenv.sh build      build the image
#   scripts/condor-execenv.sh up         start the pool container
#   scripts/condor-execenv.sh preflight  fast cgroup-delegation probe
#   scripts/condor-execenv.sh selftest   run the in-container proofs
#   scripts/condor-execenv.sh diagnose   dump container + pool logs
#   scripts/condor-execenv.sh down       stop and remove the container
#
# The repo this script lives in is mounted read-only at /repo; the
# container builds its own Linux venv in the io-work volume.
#
# IO_EXECENV_PRIVILEGED=1 launches with --privileged instead of the
# default CAP_SYS_ADMIN: GitHub-hosted runners write-protect the cgroup
# mount so no capability set can remount it there, while --privileged
# arrives with it already read-write. Ephemeral CI runners are inside
# ADR-0013's trust posture; local Docker Desktop stays least-privilege.
set -eu

IMAGE="${IO_EXECENV_IMAGE:-io-execenv:latest}"
CONTAINER="${IO_EXECENV_CONTAINER:-io-execenv}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# linux/amd64 everywhere: htcondor.org ships no arm64 debs, and the
# current LTS (with working cgroup-v2 family tracking) only exists
# there. Native on CI runners; Rosetta/qemu-emulated on Apple Silicon —
# the same posture as the macOS pool's x86_64 tarball.
PLATFORM="linux/amd64"

privilege_flags() {
    if [ "${IO_EXECENV_PRIVILEGED:-0}" = "1" ]; then
        printf -- "--privileged"
    else
        printf -- "--cap-add SYS_ADMIN"
    fi
}

case "${1:-}" in
build)
    exec docker build --platform "$PLATFORM" -t "$IMAGE" "$REPO_ROOT/docker/execenv"
    ;;
up)
    docker volume create io-work >/dev/null
    docker volume create io-spool >/dev/null
    # --stop-timeout 60: docker stop's default 10s SIGTERM-to-SIGKILL
    # defeats condor_master's clean drain (ADR-0003; #7115 finding 8).
    # The entrypoint verifies writable cgroup-v2 delegation before
    # starting anything, whichever privilege path launched it.
    # shellcheck disable=SC2046
    exec docker run -d --name "$CONTAINER" \
        --platform "$PLATFORM" \
        --stop-timeout 60 \
        --cgroupns private \
        $(privilege_flags) \
        -v io-work:/work \
        -v io-spool:/var/lib/condor \
        -v "$REPO_ROOT":/repo:ro \
        "$IMAGE"
    ;;
preflight)
    # Fast, lifecycle-free probe of the one property CI environments
    # keep getting wrong: writable cgroup v2 under this driver's launch
    # flags. Seconds, not minutes, to a definitive answer.
    # shellcheck disable=SC2046
    exec docker run --rm \
        --platform "$PLATFORM" \
        --cgroupns private \
        $(privilege_flags) \
        --entrypoint sh \
        "$IMAGE" -c '
            set -e
            [ "$(stat -f -c %T /sys/fs/cgroup)" = "cgroup2fs" ] \
                || { echo "preflight: not cgroup v2"; exit 64; }
            if ! mkdir /sys/fs/cgroup/.preflight 2>/dev/null; then
                mount -o remount,rw /sys/fs/cgroup \
                    || { echo "preflight: cgroup fs read-only and not remountable"; exit 64; }
                mkdir /sys/fs/cgroup/.preflight \
                    || { echo "preflight: cgroup fs still read-only"; exit 64; }
            fi
            rmdir /sys/fs/cgroup/.preflight
            echo "preflight: writable cgroup v2 available"
        '
    ;;
selftest)
    exec docker exec --user io "$CONTAINER" /usr/local/bin/execenv-selftest
    ;;
diagnose)
    # The lifecycle owner's diagnostic surface: consumers (the CI
    # workflow included) call this instead of reaching into docker or
    # the pool's log layout themselves (A1, #7119 review).
    echo "=== container status"
    docker ps -a --filter "name=$CONTAINER" --format '{{.Names}} {{.Status}}' || true
    echo "=== container log (tail)"
    docker logs --tail 50 "$CONTAINER" 2>&1 || true
    echo "=== pool daemon logs (tail)"
    docker exec "$CONTAINER" sh -c 'tail -n 40 /var/log/condor/*Log 2>/dev/null' \
        || echo "(pool logs unavailable - container not running)"
    exit 0
    ;;
down)
    # --type container is load-bearing: a bare inspect resolves the
    # IMAGE io-execenv:latest when no such container exists (observed
    # on the CI runner: teardown succeeded, the postcondition matched
    # the image forever and reported failure).
    if ! docker inspect --type container "$CONTAINER" >/dev/null 2>&1; then
        echo "execenv: container $CONTAINER already absent"
        exit 0
    fi
    docker stop "$CONTAINER" >/dev/null
    docker rm "$CONTAINER" >/dev/null
    # Postcondition, verified: a false "removed" would hide daemon or
    # authorization failures behind a comforting message (B4, #7119
    # review). The daemon acknowledges rm slightly asynchronously on
    # some hosts (observed on a GitHub runner: rm succeeded, an
    # immediate inspect still resolved) - poll briefly before
    # declaring failure; the honesty is in the bounded verification,
    # not in racing the daemon.
    removed=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! docker inspect --type container "$CONTAINER" >/dev/null 2>&1; then
            removed="yes"
            break
        fi
        sleep 1
    done
    if [ -z "$removed" ]; then
        echo "execenv: FAILED to remove container $CONTAINER" >&2
        exit 70
    fi
    echo "execenv: container removed (volumes io-work/io-spool kept)"
    ;;
*)
    echo "usage: $0 {build|up|preflight|selftest|diagnose|down}" >&2
    exit 64
    ;;
esac
