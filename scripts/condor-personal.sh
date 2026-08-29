#!/usr/bin/env bash
set -euo pipefail

# Manage the opt-in personal HTCondor pool for validation lanes.
#
# SCOPE (ADR-0001, docs/architecture/execenv/): on macOS this pool tracks
# process families, not cgroups - setsid-detached descendants escape
# removal. Validation lanes are non-detaching and safe here; AGENT JOBS
# ARE NOT and require the Linux execution environment.
#
#   scripts/condor-personal.sh up      # install (first run) and start
#   scripts/condor-personal.sh down    # stop the daemons
#   scripts/condor-personal.sh status  # pool and queue summary
#   scripts/condor-personal.sh env     # shell exports; eval "$(... env)"
#
# macOS: upstream ships x86_64 only, so the daemons run under Rosetta 2
# (verified working; scheduling overhead measured at ~2s). Linux: uses
# the system installation (apt-get install htcondor) with a personal
# configuration written alongside this repo's low-latency knobs.

CONDOR_VERSION="25.8.2"
CONDOR_SERIES="25.x"
POOL_HOME="${ISSUE_ORCHESTRATOR_CONDOR_HOME:-$HOME/.local/share/issue-orchestrator/condor}"
TARBALL_DIR_NAME="condor-${CONDOR_VERSION}-x86_64_macOS13-stripped"
TARBALL_URL="https://research.cs.wisc.edu/htcondor/tarball/${CONDOR_SERIES}/${CONDOR_VERSION}/release/${TARBALL_DIR_NAME}.tar.gz"

# Low-latency personal-pool tuning: validation lanes want seconds-scale
# negotiation, tight periodic-expression evaluation (deadline precision),
# and CONCURRENCY_LIMIT_DEFAULT=1 so every named limit is a machine-wide
# mutex — the contract the lane submit compiler documents.
write_lane_config() {
  local config_dir="$1"
  cat > "${config_dir}/90-issue-orchestrator-lanes.conf" <<'EOF'
NEGOTIATOR_INTERVAL = 1
NEGOTIATOR_CYCLE_DELAY = 1
NEGOTIATOR_MIN_INTERVAL = 1
SCHEDD_MIN_INTERVAL = 1
JOB_START_DELAY = 0
JOB_START_COUNT = 100
CLAIM_WORKLIFE = 3600
PERIODIC_EXPR_INTERVAL = 5
CONCURRENCY_LIMIT_DEFAULT = 1
# Per-job accounting (#7127). When a job leaves the queue the schedd
# writes its COMPLETE final ClassAd to <dir>/history.<cluster>.<proc>.
# The lane runner collects that file into a failed lane's retained
# diagnostics, so the full accounting - exit status, peak memory, CPU
# usage, slot, hold reason, every timestamp - survives WITH the run
# directory instead of only inside a rotating global history file that
# nothing correlates back to the lane. Configuration and collection
# only: no new accounting code anywhere.
PER_JOB_HISTORY_DIR = $(SPOOL)/per-job-history
# Lane compatibility, required on EVERY pool this helper manages (not
# just ones it builds the role for): condor bind-mounts job scratch
# over /tmp by default on Linux, so a lane whose working directory
# lives under the real /tmp fails with errno=2 "Cannot access initial
# working directory". Lanes must see the submitter's real filesystem.
MOUNT_UNDER_SCRATCH =
EOF
  write_load_backoff_config "$config_dir"
  write_capacity_config "$config_dir"
  # LAST, deliberately: the capacity writer validates and rejects a
  # malformed dial, and `set -e` aborts here before an intent record
  # could claim a policy that was never installed.
  write_policy_intent_config "$config_dir"
}

# The pool's own record of what it was BUILT to carry.
#
# The two opt-ins above are environment variables read exactly once,
# here, at `up` time, by this process. Nothing else recorded that they
# were set - so no later reader could tell a pool that deliberately
# opted out of the backoff policy from one whose policy file was
# removed by hand. That gap made the preflight check exit 0 on a pool
# started with IO_CONDOR_LOAD_BACKOFF=1 whose 91- file had been
# deleted: a reproduced false green (C1, #7132 review).
#
# Persisting intent as CONFIG MACROS is what closes it. Intent then
# travels the identical staging/install/reconcile path as the policies
# it describes, and is readable over the same condor_config_val
# channel the check already uses - no sidecar file, no second
# discovery mechanism, nothing that can be installed out of step with
# what it describes.
#
# IO_INTENT_LOAD_BACKOFF is written in BOTH states on purpose. It is
# the sentinel that says an intent record exists at all, so a pool
# predating this file reads as legacy and is reported as drift rather
# than silently trusted.
write_policy_intent_config() {
  local config_dir="$1"
  {
    echo "# Written by scripts/condor-personal.sh: what this pool was built"
    echo "# to carry. Read by the lane preflight check, which asserts each"
    echo "# managed policy file is present if and only if it was intended."
    if [ "${IO_CONDOR_LOAD_BACKOFF:-0}" = "1" ]; then
      echo "IO_INTENT_LOAD_BACKOFF = True"
    else
      echo "IO_INTENT_LOAD_BACKOFF = False"
    fi
    # Left UNDEFINED (no line at all) when no dial was asked for: an
    # empty assignment reads back as "Not defined" from the config
    # tool anyway, so absence is the only encoding that says the same
    # thing on both sides. Normalized to base 10 to match the value
    # write_capacity_config actually used.
    if [ -n "${IO_POOL_CAPACITY_PERCENT:-}" ]; then
      echo "IO_INTENT_CAPACITY_PERCENT = $(( 10#$IO_POOL_CAPACITY_PERCENT ))"
    fi
  } > "${config_dir}/90-io-policy-intent.conf"
}

# One throughput dial (IO_POOL_CAPACITY_PERCENT at `up` time): the
# pool's admission capacity as a percentage of physical cores. Lane
# CPU requests are MEASURED demand, and most lanes are I/O-bound, so
# 100% of physical cores is a conservative default — raising the dial
# admits more concurrent lanes (deliberate oversubscription), lowering
# it throttles the whole pool uniformly. This is the static half of
# load control; the load-backoff policy above is the reactive half.
# Unset means condor's own physical detection (no file). Takes effect
# at `up`, when the startd re-detects its resources.
write_capacity_config() {
  local config_dir="$1"
  if [ -z "${IO_POOL_CAPACITY_PERCENT:-}" ]; then
    rm -f "${config_dir}/92-io-pool-capacity.conf"
    return 0
  fi
  case "$IO_POOL_CAPACITY_PERCENT" in
    ''|*[!0-9]*)
      echo "condor-personal: IO_POOL_CAPACITY_PERCENT must be a positive integer percentage, got '${IO_POOL_CAPACITY_PERCENT}'" >&2
      return 64
      ;;
  esac
  # Normalize to base 10 BEFORE any arithmetic: bash treats a leading
  # zero as octal, so an unnormalized "08" passes the digits-only
  # check and then dies with "value too great for base" (B2, #7122
  # review). The normalized value is the only one used from here on.
  local capacity_percent physical_cores scaled_cores
  capacity_percent=$(( 10#$IO_POOL_CAPACITY_PERCENT ))
  if [ "$capacity_percent" -lt 1 ]; then
    echo "condor-personal: IO_POOL_CAPACITY_PERCENT must be at least 1, got '${IO_POOL_CAPACITY_PERCENT}'" >&2
    return 64
  fi
  physical_cores=$(sysctl -n hw.ncpu 2>/dev/null || nproc)
  scaled_cores=$(( physical_cores * capacity_percent / 100 ))
  if [ "$scaled_cores" -lt 1 ]; then
    scaled_cores=1
  fi
  cat > "${config_dir}/92-io-pool-capacity.conf" <<EOF
# ${capacity_percent}% of ${physical_cores} physical cores.
NUM_CPUS = ${scaled_cores}
EOF
}

# Opt-in machine-load backoff (IO_CONDOR_LOAD_BACKOFF=1 at `up` time):
# freeze eligible running lanes when the machine's OWNER load - load
# condor's own jobs did not cause - climbs, thaw when it clears. Three
# non-negotiable rules, each learned in design review:
#   1. Key on owner load, never total load: SUSPEND over total LoadAvg
#      would trip on the gate's own lane fan and oscillate against its
#      own reflection.
#   2. Only lanes that declared themselves suspendable may freeze - a
#      live provider exchange frozen mid-turn thaws into a manufactured
#      provider-outage failure.
#   3. The compiled lane deadline subtracts CumulativeSuspensionTime,
#      so frozen time never burns a lane's budget (submit_compiler.py
#      owns that half of the contract).
write_load_backoff_config() {
  local config_dir="$1"
  if [ "${IO_CONDOR_LOAD_BACKOFF:-0}" != "1" ]; then
    # Symmetric lifecycle: opting out removes the policy this helper
    # previously wrote, or "off by default" is only true before the
    # first opt-in (B2, #7118 review).
    rm -f "${config_dir}/91-io-load-backoff.conf"
    return 0
  fi
  # TotalLoadAvg/TotalCondorLoadAvg are the MACHINE-wide pair; the
  # unprefixed LoadAvg/CondorLoadAvg are per-slot on multi-core
  # machines and would make different suspension decisions for jobs on
  # the same host (B1, #7118 review — verified live: two busy dynamic
  # slots on one 18-core host advertised LoadAvg 3.16 and 0.0 while
  # TotalLoadAvg was 3.16).
  cat > "${config_dir}/91-io-load-backoff.conf" <<EOF
OwnerLoadAvg = (TotalLoadAvg - TotalCondorLoadAvg)
WANT_SUSPEND = (TARGET.SuspendableLane =?= True)
SUSPEND = (\$(OwnerLoadAvg) > ${IO_CONDOR_SUSPEND_LOAD:-5.0}) && (TARGET.SuspendableLane =?= True)
CONTINUE = (\$(OwnerLoadAvg) < ${IO_CONDOR_CONTINUE_LOAD:-2.0})
EOF
}

# The schedd writes the per-job ClassAds itself, so the directory must
# exist and be writable by whoever runs it - the submitting user on the
# tarball pools, the condor user on a system install. Condor does not
# create it, and a missing directory disables per-job accounting
# silently, so this runs at every `up` and is never assumed.
ensure_per_job_history_dir() {
  local spool history owner
  spool=$(condor_config_val SPOOL 2>/dev/null || echo "")
  if [ -z "$spool" ]; then
    echo "condor-personal: SPOOL is unset; per-job accounting is off" >&2
    return 0
  fi
  history="$spool/per-job-history"
  if [ -d "$history" ]; then
    return 0
  fi
  if mkdir -p "$history" 2>/dev/null; then
    return 0
  fi
  # Accounting is a diagnostic aid, not a precondition for running
  # lanes: a pool whose spool we cannot write loses the ClassAds, not
  # `up`.
  owner=$(stat -f '%Su' "$spool" 2>/dev/null || stat -c '%U' "$spool" 2>/dev/null || echo "")
  if ! sudo mkdir -p "$history" 2>/dev/null; then
    echo "condor-personal: could not create $history; per-job accounting is off" >&2
    return 0
  fi
  if [ -n "$owner" ]; then
    sudo chown "$owner" "$history" || true
  fi
}

# Files this helper manages whose ABSENCE from staging is meaningful:
# copying staged files over a destination cannot delete anything, so a
# previously-installed opt-in policy would survive every later plain
# `up`. The install boundary owns that reconciliation (B2, #7118
# review): a managed file not present in staging is removed from the
# destination.
MANAGED_OPTIONAL_CONFIGS="91-io-load-backoff.conf 92-io-pool-capacity.conf"

install_staged_configs() {
  local staging="$1" destination="$2" runner=""
  if [ ! -w "$destination" ]; then
    runner="sudo"
  fi
  $runner cp "$staging"/*.conf "$destination/"
  for managed in $MANAGED_OPTIONAL_CONFIGS; do
    if [ ! -f "$staging/$managed" ] && [ -f "$destination/$managed" ]; then
      $runner rm -f "$destination/$managed"
    fi
  done
}

# The plain Linux htcondor package boots DAEMON_LIST = MASTER and
# nothing else - it is a component install, not a personal pool. This
# overlay defines the personal role explicitly: all five daemons on
# loopback, with CONDOR_HOST paired so discovery stays consistent
# (loopback without a matching CONDOR_HOST strands clients - learned
# the hard way in both directions).
write_personal_role_config() {
  local config_dir="$1"
  cat > "${config_dir}/85-io-personal-role.conf" <<'ROLECONF'
CONDOR_HOST = 127.0.0.1
COLLECTOR_HOST = 127.0.0.1
NETWORK_INTERFACE = 127.0.0.1
BIND_ALL_INTERFACES = False
DAEMON_LIST = MASTER COLLECTOR NEGOTIATOR SCHEDD STARTD
# Personal pool = single trusted user: jobs run as the submitting
# owner, matching the tarball pools. Without this, system installs run
# jobs as the slot user (nobody), which cannot access the submitter's
# 0700 working directories - every lane goes on hold with
# "Cannot access initial working directory".
UID_DOMAIN = $(FULL_HOSTNAME)
TRUST_UID_DOMAIN = TRUE
STARTER_ALLOW_RUNAS_OWNER = TRUE
SEC_DEFAULT_AUTHENTICATION_METHODS = FS, IDTOKENS
ALLOW_READ = *
ALLOW_WRITE = $(CONDOR_HOST) $(IP_ADDRESS) 127.0.0.1
ALLOW_DAEMON = $(ALLOW_WRITE)
ROLECONF
}

# Assert the running pool actually has the personal role: every daemon
# a lane needs must be in the effective DAEMON_LIST, or fail loudly.
assert_personal_role() {
  local daemons missing=""
  daemons=$(condor_config_val DAEMON_LIST 2>/dev/null || echo "")
  for required in COLLECTOR NEGOTIATOR SCHEDD STARTD; do
    case "$daemons" in *"$required"*) ;; *) missing="$missing $required";; esac
  done
  if [ -n "$missing" ]; then
    echo "condor-personal: DAEMON_LIST='$daemons' is missing$missing - not a personal pool" >&2
    exit 70
  fi
}

# Per-job scratch directories become the job's TMPDIR. They must live
# OUTSIDE $HOME: the repo's home-isolation guardrails wall agent work
# off from real home configs, and a scratch dir under $HOME would put
# every lane's tmp files inside the walled-off area.
EXECUTE_DIR="/private/tmp/issue-orchestrator-condor-execute-$(id -u)"

darwin_install() {
  mkdir -p "$POOL_HOME"
  if [ ! -x "$POOL_HOME/$TARBALL_DIR_NAME/sbin/condor_master" ]; then
    echo "condor-personal: downloading HTCondor ${CONDOR_VERSION} (x86_64, runs under Rosetta 2)"
    arch -x86_64 /usr/bin/true 2>/dev/null || {
      echo "condor-personal: Rosetta 2 is required: softwareupdate --install-rosetta" >&2
      exit 78
    }
    curl -sfL -o "$POOL_HOME/condor.tar.gz" "$TARBALL_URL"
    tar xzf "$POOL_HOME/condor.tar.gz" -C "$POOL_HOME"
    rm "$POOL_HOME/condor.tar.gz"
    (cd "$POOL_HOME/$TARBALL_DIR_NAME" && ./bin/make-personal-from-tarball)
  fi
  write_lane_config "$POOL_HOME/$TARBALL_DIR_NAME/local/config.d"
  mkdir -p "$EXECUTE_DIR"
  {
    printf 'EXECUTE = %s\n' "$EXECUTE_DIR"
    # Darwin-only: a roaming laptop pool must bind loopback so daemon
    # addresses never go stale when wifi changes (idle-forever jobs
    # after joining a new network were the symptom). NOT applied to
    # Linux system installs: there NETWORK_INTERFACE=127.0.0.1 without
    # matching CONDOR_HOST/collector settings strands discovery and
    # the pool never reports ready.
    printf 'NETWORK_INTERFACE = 127.0.0.1\n'
  } >> "$POOL_HOME/$TARBALL_DIR_NAME/local/config.d/90-issue-orchestrator-lanes.conf"
  export CONDOR_CONFIG="$POOL_HOME/$TARBALL_DIR_NAME/etc/condor_config"
  export PATH="$POOL_HOME/$TARBALL_DIR_NAME/bin:$POOL_HOME/$TARBALL_DIR_NAME/sbin:$PATH"
}

# LOCAL_CONFIG_DIR may be a comma-separated LIST (Ubuntu 24.04 returns
# "/usr/share/condor/config.d,/etc/condor/config.d/"). Select one real
# directory from it: the first existing writable entry, else the first
# entry under /etc (the local-admin location), else the first entry.
select_config_dir() {
  local raw="$1" entry fallback="" etc_entry=""
  IFS=',' read -ra entries <<< "$raw"
  for entry in "${entries[@]}"; do
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    entry="${entry%/}"
    [ -n "$entry" ] || continue
    [ -n "$fallback" ] || fallback="$entry"
    if [ -z "$etc_entry" ] && [[ "$entry" == /etc/* ]]; then
      etc_entry="$entry"
    fi
    if [ -d "$entry" ] && [ -w "$entry" ]; then
      echo "$entry"
      return 0
    fi
  done
  if [ -n "$etc_entry" ]; then
    echo "$etc_entry"
    return 0
  fi
  [ -n "$fallback" ] && echo "$fallback" && return 0
  return 1
}

# Readiness is not daemon topology - it is the ability to execute a
# lane. This probe submits a minimal job whose working directory is a
# fresh submitter-owned 0700 directory under the real /tmp (the exact
# shape every pytest lane uses) and requires it to write a marker
# there. It fails fast at startup - with the effective identity
# configuration and any hold reasons - instead of letting every later
# lane go on hold.
assert_execution_invariant() {
  local probe_dir marker cluster deadline
  probe_dir=$(mktemp -d "${TMPDIR:-/tmp}/io-condor-probe-XXXXXX")
  chmod 700 "$probe_dir"
  marker="$probe_dir/proof"
  cat > "$probe_dir/probe.sh" <<'PROBE'
#!/bin/sh
echo alive > proof
PROBE
  chmod +x "$probe_dir/probe.sh"
  cat > "$probe_dir/probe.sub" <<PROBESUB
executable = $probe_dir/probe.sh
universe = vanilla
initialdir = $probe_dir
output = $probe_dir/out
error = $probe_dir/err
log = $probe_dir/log
request_cpus = 1
should_transfer_files = NO
queue
PROBESUB
  local submit_output
  if ! submit_output=$(condor_submit -terse "$probe_dir/probe.sub" 2>&1); then
    echo "condor-personal: execution probe could not submit: $submit_output" >&2
    rm -rf "$probe_dir"
    exit 70
  fi
  cluster=$(printf '%s\n' "$submit_output" | awk -F. '{print $1; exit}')
  if [ -z "$cluster" ]; then
    echo "condor-personal: execution probe submit produced no cluster id: $submit_output" >&2
    rm -rf "$probe_dir"
    exit 70
  fi
  deadline=$((SECONDS + ${IO_CONDOR_PROBE_TIMEOUT:-60}))
  while [ "$SECONDS" -lt "$deadline" ]; do
    [ -s "$marker" ] && break
    sleep 1
  done
  if [ ! -s "$marker" ]; then
    echo "condor-personal: execution probe FAILED - the pool cannot run a lane in a submitter-owned directory" >&2
    echo "--- identity and namespace configuration (effective values and origins):" >&2
    condor_config_val -v UID_DOMAIN TRUST_UID_DOMAIN STARTER_ALLOW_RUNAS_OWNER SLOT1_USER MOUNT_UNDER_SCRATCH 2>&1 | sed 's/^/config: /' >&2 || true
    if command -v systemctl >/dev/null 2>&1; then
      echo "systemd PrivateTmp: $(systemctl show condor -p PrivateTmp --value 2>/dev/null)" >&2
    fi
    echo "--- held/queued probe state:" >&2
    condor_q "$cluster" -af:jh JobStatus HoldReason Owner 2>&1 | sed 's/^/probe: /' >&2 || true
    echo "--- StarterLog tail:" >&2
    starterlog=$(condor_config_val LOG 2>/dev/null)/StarterLog.slot1_1
    [ -f "$starterlog" ] || starterlog=$(condor_config_val LOG 2>/dev/null)/StarterLog
    [ -f "$starterlog" ] && tail -15 "$starterlog" | sed 's/^/starter: /' >&2
    condor_rm "$cluster" >/dev/null 2>&1 || true
    rm -rf "$probe_dir"
    exit 70
  fi
  condor_rm "$cluster" >/dev/null 2>&1 || true
  rm -rf "$probe_dir"
  echo "condor-personal: execution probe ok (lane ran in a submitter-owned directory)"
}

# A component install (no SCHEDD in the ambient DAEMON_LIST) needs the
# full personal role written; a complete ambient pool keeps its own
# topology and receives only the always-applied lane config.
ambient_needs_personal_role() {
  case "$1" in
    *SCHEDD*) return 1 ;;
    *) return 0 ;;
  esac
}

linux_configure() {
  command -v condor_master >/dev/null 2>&1 || {
    echo "condor-personal: install HTCondor first: sudo apt-get install htcondor" >&2
    exit 78
  }
  local config_dir_list config_dir staging
  config_dir_list=$(condor_config_val LOCAL_CONFIG_DIR 2>/dev/null | head -1)
  if [ -z "$config_dir_list" ]; then
    echo "condor-personal: LOCAL_CONFIG_DIR is not configured" >&2
    exit 70
  fi
  config_dir=$(select_config_dir "$config_dir_list") || {
    echo "condor-personal: no usable entry in LOCAL_CONFIG_DIR=$config_dir_list" >&2
    exit 70
  }
  if [ ! -d "$config_dir" ]; then
    sudo mkdir -p "$config_dir"
  fi
  staging=$(mktemp -d)
  write_lane_config "$staging"
  ambient_daemons=$(condor_config_val DAEMON_LIST 2>/dev/null || echo "")
  if ambient_needs_personal_role "$ambient_daemons"; then
    write_personal_role_config "$staging"
  fi
  install_staged_configs "$staging" "$config_dir"
  rm -rf "$staging"
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart condor
  else
    condor_reconfig >/dev/null 2>&1 || true
  fi
}

resolve_platform_env() {
  if [ "$(uname -s)" = "Darwin" ]; then
    export CONDOR_CONFIG="$POOL_HOME/$TARBALL_DIR_NAME/etc/condor_config"
    export PATH="$POOL_HOME/$TARBALL_DIR_NAME/bin:$POOL_HOME/$TARBALL_DIR_NAME/sbin:$PATH"
  fi
}

await_pool_ready() {
  for _ in $(seq 1 60); do
    if condor_status -total >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "condor-personal: pool did not become ready within 60s" >&2
  echo "--- diagnostics -------------------------------------------" >&2
  condor_config_val CONDOR_HOST NETWORK_INTERFACE COLLECTOR_HOST DAEMON_LIST 2>&1 | sed 's/^/config: /' >&2 || true
  pgrep -fl condor_ 2>/dev/null | sed 's/^/proc: /' >&2 || echo "proc: no condor daemons running" >&2
  if command -v systemctl >/dev/null 2>&1; then
    systemctl status condor --no-pager 2>&1 | tail -5 | sed 's/^/systemd: /' >&2 || true
  fi
  for log in MasterLog CollectorLog StartLog; do
    logpath=$(condor_config_val LOG 2>/dev/null)/$log
    [ -f "$logpath" ] && { echo "--- tail $log:" >&2; tail -5 "$logpath" >&2; }
  done
  exit 70
}

# When sourced (tests), expose the functions without dispatching.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0 2>/dev/null || true
fi

case "${1:-}" in
  up)
    if [ "$(uname -s)" = "Darwin" ]; then
      darwin_install
    else
      linux_configure
    fi
    ensure_per_job_history_dir
    if condor_status -total >/dev/null 2>&1; then
      echo "condor-personal: pool already running; applying configuration"
      condor_reconfig >/dev/null 2>&1 || true
      condor_restart -startd >/dev/null 2>&1 || true
    else
      if ! pgrep -x condor_master >/dev/null 2>&1; then
        condor_master
      fi
      echo "condor-personal: pool starting"
    fi
    await_pool_ready
    assert_personal_role
    assert_execution_invariant
    condor_status -total
    ;;
  down)
    resolve_platform_env
    condor_off -master 2>/dev/null || echo "condor-personal: pool was not running"
    ;;
  status)
    resolve_platform_env
    condor_status -total && condor_q -totals
    ;;
  env)
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "export CONDOR_CONFIG=\"$POOL_HOME/$TARBALL_DIR_NAME/etc/condor_config\""
      echo "export PATH=\"$POOL_HOME/$TARBALL_DIR_NAME/bin:$POOL_HOME/$TARBALL_DIR_NAME/sbin:\$PATH\""
    fi
    ;;
  *)
    echo "usage: $0 {up|down|status|env}" >&2
    exit 64
    ;;
esac
