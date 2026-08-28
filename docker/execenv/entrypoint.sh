#!/bin/sh
# PID 1 of the execution environment (ADR-0003): condor_master, after
# proving the kernel actually offers what this container exists for.
set -eu

fatal() {
    echo "execenv: FATAL: $*" >&2
    exit 64
}

# ADR-0001 calls cgroup containment "the whole point"; issue #7115
# review finding 9 says fail, never warn. Two conditions, both fatal:
# cgroup v2 must be mounted, AND its controllers must be delegatable.
# Presence alone is vacuous - a bare container passes the mount check
# while condor silently degrades to non-cgroup tracking ("Cgroup // is
# not writeable, cannot use cgroups") and the escape-containment
# guarantee is void. Proven empirically in this container, 2026-08-28.
[ "$(stat -f -c %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ] \
    || fatal "/sys/fs/cgroup is not cgroup v2 (ADR-0001); refusing to start"

# The delegation dance (cgroup v2 no-internal-processes rule): a cgroup
# cannot both hold processes and delegate controllers to children, and
# docker starts us in the root cgroup. Move ourselves into a leaf,
# then enable controllers for our siblings - including the "htcondor"
# base group the starter demands be writable before it will track jobs.
CGROUP_ROOT=/sys/fs/cgroup
# Docker Desktop mounts the cgroup fs read-only even in a private
# namespace; the run recipe grants CAP_SYS_ADMIN for exactly this
# remount and nothing else (least privilege that works - --privileged
# is not needed and not used).
mount -o remount,rw "$CGROUP_ROOT" \
    || fatal "cannot remount $CGROUP_ROOT read-write (run with --cap-add SYS_ADMIN)"
mkdir -p "$CGROUP_ROOT/init" "$CGROUP_ROOT/htcondor"
echo $$ > "$CGROUP_ROOT/init/cgroup.procs" \
    || fatal "cannot move PID 1 out of the root cgroup"
# cpuset and io included: the starter enables them on the job group
# and an unavailable controller surfaces as a misleading ENOENT from
# subtree_control (observed live before this line grew).
WANTED="+cpuset +cpu +io +memory +pids"
echo "$WANTED" > "$CGROUP_ROOT/cgroup.subtree_control" \
    || fatal "cannot delegate controllers at the cgroup root"
echo "$WANTED" > "$CGROUP_ROOT/htcondor/cgroup.subtree_control" \
    || fatal "cannot delegate controllers under the htcondor base group"

# Verify the property, not the plumbing: memory delegation must be live
# where the starter will create job groups.
grep -q memory "$CGROUP_ROOT/htcondor/cgroup.subtree_control" \
    || fatal "memory controller not delegated under htcondor"

command -v condor_master >/dev/null 2>&1 \
    || fatal "condor_master not installed"

exec condor_master -f
