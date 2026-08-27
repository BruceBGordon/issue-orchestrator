#!/usr/bin/env bash
set -euo pipefail

# Manage the opt-in personal HTCondor pool for validation lanes.
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
# A laptop pool must survive network roaming: bind loopback so daemon
# addresses never go stale when wifi changes (idle-forever jobs after
# joining a new network were the symptom).
NETWORK_INTERFACE = 127.0.0.1
NEGOTIATOR_INTERVAL = 1
NEGOTIATOR_CYCLE_DELAY = 1
NEGOTIATOR_MIN_INTERVAL = 1
SCHEDD_MIN_INTERVAL = 1
JOB_START_DELAY = 0
JOB_START_COUNT = 100
CLAIM_WORKLIFE = 3600
PERIODIC_EXPR_INTERVAL = 5
CONCURRENCY_LIMIT_DEFAULT = 1
EOF
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
  printf 'EXECUTE = %s\n' "$EXECUTE_DIR" \
    >> "$POOL_HOME/$TARBALL_DIR_NAME/local/config.d/90-issue-orchestrator-lanes.conf"
  export CONDOR_CONFIG="$POOL_HOME/$TARBALL_DIR_NAME/etc/condor_config"
  export PATH="$POOL_HOME/$TARBALL_DIR_NAME/bin:$POOL_HOME/$TARBALL_DIR_NAME/sbin:$PATH"
}

linux_configure() {
  command -v condor_master >/dev/null 2>&1 || {
    echo "condor-personal: install HTCondor first: sudo apt-get install htcondor" >&2
    exit 78
  }
  local config_dir staging
  config_dir=$(condor_config_val LOCAL_CONFIG_DIR 2>/dev/null | head -1)
  if [ -z "$config_dir" ]; then
    echo "condor-personal: LOCAL_CONFIG_DIR is not configured" >&2
    exit 70
  fi
  staging=$(mktemp -d)
  write_lane_config "$staging"
  if [ -w "$config_dir" ]; then
    cp "$staging/90-issue-orchestrator-lanes.conf" "$config_dir/"
  else
    sudo cp "$staging/90-issue-orchestrator-lanes.conf" "$config_dir/"
  fi
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
  exit 70
}

case "${1:-}" in
  up)
    if [ "$(uname -s)" = "Darwin" ]; then
      darwin_install
    else
      linux_configure
    fi
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
