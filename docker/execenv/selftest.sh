#!/bin/sh
# In-container proof that the execution environment delivers what it
# exists for (issue #7115, run via `scripts/condor-execenv.sh selftest`):
#
#   1. the pool schedules at all (probe job from a 0700 directory);
#   2. the LaneExecutor contract holds inside the container — including
#      the boundary test's Linux branch: a setsid-detached grandchild
#      is CONTAINED here, the exact guarantee macOS cannot give;
#   3. request_memory is a hard ceiling: the same allocation succeeds
#      under a sufficient budget and dies under an insufficient one, so
#      the limit is the declaration, not the machine.
#
# The repo is mounted read-only at /repo; its Linux venv is built into
# /work/venv (never the repo's own macOS .venv).
set -eu

say() { printf 'execenv-selftest: %s\n' "$*"; }

say "waiting for the schedd"
attempts=0
until condor_q >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -le 60 ] || { say "FATAL: schedd never answered"; exit 1; }
    sleep 1
done

say "probe job from a 0700 directory"
probe_dir="$(mktemp -d)"
chmod 0700 "$probe_dir"
cat > "$probe_dir/probe.sub" <<EOF
universe = vanilla
executable = /bin/true
initialdir = $probe_dir
log = $probe_dir/probe.log
queue
EOF
condor_submit "$probe_dir/probe.sub" >/dev/null
attempts=0
until grep -q "Job terminated" "$probe_dir/probe.log" 2>/dev/null; do
    attempts=$((attempts + 1))
    [ "$attempts" -le 60 ] || {
        say "FATAL: probe never ran"
        condor_q -analyze || true
        exit 1
    }
    sleep 1
done
say "probe completed"

say "building the Linux venv from the read-only repo"
cd /repo
export UV_PROJECT_ENVIRONMENT=/work/venv
export UV_CACHE_DIR=/work/uv-cache
# The dev extra holds pytest (optional-dependencies.dev, not a default
# group) - the proofs need it.
uv sync --frozen --no-editable --extra dev
PYTHON=/work/venv/bin/python

say "lane executor contract suite against the container pool"
cd /work
# requires_backoff_pool excluded: that acceptance test needs a pool
# started with the load-backoff policy; this container's pool
# deliberately runs without one, and the test fails its own preflight
# here by design (same exclusion as the condor-lanes CI job).
"$PYTHON" -m pytest /repo/tests/integration/test_condor_lane_executor.py \
    -q -m "requires_infra and not requires_backoff_pool" -p no:cacheprovider \
    --rootdir=/repo -o addopts=

# The allocation waits 10s before asking for memory: the starter
# attaches the job to its limit-bearing cgroup shortly AFTER spawn,
# and under emulation that window stretches to seconds. A workload
# that front-loads allocation inside the window escapes the ceiling -
# a real, documented residual of this stack (see the PR notes), not
# something this proof should race against. The proof's claim is that
# the settled ceiling is kernel-enforced, not advisory.
ALLOCATE="import time; time.sleep(10); block = bytearray(400 * 1024 * 1024); print(len(block))"

# lane-run resolves scheduling facts (cpus, memory, suspendability)
# from .issue-orchestrator/lanes.yaml under its WORKING directory —
# and /work is deliberately not a git repository so the runtime
# history and dispatch journal stay inert (the repo mount is
# read-only; they could not write there anyway). Copy the
# declarations beside the working dir so resolution finds them.
mkdir -p /work/.issue-orchestrator
cp /repo/.issue-orchestrator/lanes.yaml /work/.issue-orchestrator/

say "memory ceiling: sufficient budget must succeed"
LANE_RUN="$PYTHON -m issue_orchestrator.entrypoints.cli_tools.lane_run"
$LANE_RUN --backend condor --work-key execenv.memory-ok \
    --timeout-seconds 120 -- \
    "$PYTHON" -c "$ALLOCATE" \
    || { say "FATAL: a 400MB workload died under a 1024MB budget"; exit 1; }

say "memory ceiling: insufficient budget must be enforced by the kernel"
if $LANE_RUN --backend condor --work-key execenv.memory-oom \
    --timeout-seconds 120 -- \
    "$PYTHON" -c "$ALLOCATE"
then
    say "FATAL: a 400MB workload survived a 128MB budget - the ceiling is advisory"
    exit 1
fi
say "memory ceiling enforced"

say "ALL PROOFS PASSED"
