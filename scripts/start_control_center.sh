#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV_PATH="${CC_VENV_PATH:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON:-python3}"
PORT="${CC_PORT:-19080}"

# Environment overrides:
#   CC_PORT: Control Center port (default: 19080)
#   CC_VENV_PATH: Python environment to create, sync, and launch (default: <repo>/.venv)

# ---------------------------------------------------------------------------
# Stop all running orchestrator processes (control centers, agents, validators)
# ---------------------------------------------------------------------------
stop_all_orchestrators() {
  echo "=== Stopping all orchestrator processes ==="
  local found_any=false

  # 1. Control centers (any port)
  local cc_pids
  cc_pids=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center' 2>/dev/null || echo "")
  if [[ -n "${cc_pids}" ]]; then
    found_any=true
    local cc_count
    cc_count=$(echo "${cc_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${cc_count} control center(s)..."
    echo "${cc_pids}" | xargs kill 2>/dev/null || true
  fi

  # 2. Repository engine/orchestrator processes
  local orch_pids
  orch_pids=$(pgrep -f 'issue_orchestrator\.entrypoints\.run_orchestrator' 2>/dev/null || echo "")
  if [[ -n "${orch_pids}" ]]; then
    found_any=true
    local orch_count
    orch_count=$(echo "${orch_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${orch_count} orchestrator engine process(es)..."
    echo "${orch_pids}" | xargs kill 2>/dev/null || true
  fi

  # 3. Claude agent sessions (orchestrator-spawned)
  local agent_pids
  agent_pids=$(pgrep -f 'claude.*--permission-mode bypassPermissions' 2>/dev/null || echo "")
  if [[ -n "${agent_pids}" ]]; then
    found_any=true
    local agent_count
    agent_count=$(echo "${agent_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${agent_count} Claude agent session(s)..."
    echo "${agent_pids}" | xargs kill 2>/dev/null || true
  fi

  # 4. Validate runners
  local vr_pids
  vr_pids=$(pgrep -f 'issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
  if [[ -n "${vr_pids}" ]]; then
    found_any=true
    local vr_count
    vr_count=$(echo "${vr_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${vr_count} validate runner(s)..."
    echo "${vr_pids}" | xargs kill 2>/dev/null || true
  fi

  # 5. Playwright drivers (spawned by orchestrator)
  local pw_pids
  pw_pids=$(pgrep -f 'playwright/driver.*run-driver' 2>/dev/null || echo "")
  if [[ -n "${pw_pids}" ]]; then
    found_any=true
    echo "  Killing Playwright driver(s)..."
    echo "${pw_pids}" | xargs kill 2>/dev/null || true
  fi

  if [[ "${found_any}" == "false" ]]; then
    echo "  No orchestrator processes found"
    return 0
  fi

  # Wait for processes to exit (up to 5 seconds)
  echo "  Waiting for processes to exit..."
  local remaining
  for _ in {1..10}; do
    sleep 0.5
    remaining=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center|issue_orchestrator\.entrypoints\.run_orchestrator|claude.*--permission-mode bypassPermissions|issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
    if [[ -z "${remaining}" ]]; then
      echo "  All processes stopped"
      return 0
    fi
  done

  # Stragglers get SIGKILL
  echo "  Some processes didn't exit gracefully, sending SIGKILL..."
  echo "${remaining}" | xargs kill -9 2>/dev/null || true
  sleep 1

  remaining=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center|issue_orchestrator\.entrypoints\.run_orchestrator|claude.*--permission-mode bypassPermissions|issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
  if [[ -n "${remaining}" ]]; then
    echo "  WARNING: Some processes survived SIGKILL: ${remaining}"
  else
    echo "  All processes stopped (required SIGKILL)"
  fi
}

# ---------------------------------------------------------------------------
# Pull latest code into the base repo
# ---------------------------------------------------------------------------
git_pull() {
  echo "=== Pulling latest code ==="
  local current_branch
  current_branch=$(cd "${ROOT_DIR}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

  if [[ "${current_branch}" != "main" && "${current_branch}" != "master" ]]; then
    echo "ERROR: Base repo is on branch '${current_branch}', not main." >&2
    echo "  The orchestrator must run from latest main." >&2
    echo "  Fix: cd ${ROOT_DIR} && git checkout main" >&2
    exit 1
  fi

  if ! (cd "${ROOT_DIR}" && git diff --quiet && git diff --cached --quiet); then
    echo "ERROR: Base repo has uncommitted changes." >&2
    echo "  The orchestrator must run from a clean main." >&2
    echo "  Fix: cd ${ROOT_DIR} && git stash  (or git checkout .)" >&2
    exit 1
  fi

  if ! (cd "${ROOT_DIR}" && git pull --ff-only); then
    echo "ERROR: git pull --ff-only failed." >&2
    echo "  Local main has diverged from remote." >&2
    echo "  Fix: cd ${ROOT_DIR} && git reset --hard origin/main" >&2
    exit 1
  fi
}

create_cc_snapshot() {
  # ORDERING INVARIANT — this MUST run after ``stop_all_orchestrators``
  # and before ``exec``. ``clean`` inside the Python helper skips
  # snapshot dirs whose ``cc.pid`` marker references a live process,
  # but a snapshot created here without its marker yet would be a
  # momentary orphan; because we always follow ``stop_all_orchestrators``
  # the window is closed. If this call is ever reordered, a concurrent
  # launch could race and delete a live CC's snapshot.
  #
  # Freezes ${ROOT_DIR}/src into a snapshot dir, then prepends it to
  # PYTHONPATH so every ``coding-done``/``reviewer-done``/hook invocation
  # (from the CC and every agent subprocess that inherits its env) reads
  # the frozen copy instead of whatever the base repo happens to be on
  # right now. Without this, a base-repo branch switch after CC launch
  # silently changes what every agent imports — the root cause of the
  # tixmeup-243 stale-code incident that motivated issue #5950.
  echo "=== Creating frozen source snapshot ==="
  local snapshot_pythonpath_entry
  snapshot_pythonpath_entry=$(
    "${VENV_PATH}/bin/python" -m issue_orchestrator.infra.cc_snapshot \
      create --root "${ROOT_DIR}"
  )
  export SNAPSHOT_PYTHONPATH_ENTRY="${snapshot_pythonpath_entry}"
  echo "  PYTHONPATH entry: ${SNAPSHOT_PYTHONPATH_ENTRY}"

  # Claim ownership of this snapshot dir with our own PID. After
  # ``exec`` below, the process replacing this shell keeps the same
  # PID, so the marker remains valid for the full CC lifetime — a
  # future ``clean`` call will then refuse to delete this dir while
  # the CC is alive.
  local snapshot_dir
  snapshot_dir="$(dirname "${SNAPSHOT_PYTHONPATH_ENTRY}")"
  echo "$$" > "${snapshot_dir}/cc.pid"
}

ensure_venv() {
  echo "=== Ensuring Python environment ==="
  echo "  Venv: ${VENV_PATH}"
  if [[ ! -x "${VENV_PATH}/bin/python" ]]; then
    echo "Creating venv at ${VENV_PATH}"
    "${PYTHON_BIN}" -m venv "${VENV_PATH}"
  fi
}

deps_marker_path() {
  printf '%s/.deps-synced\n' "${VENV_PATH}"
}

deps_fingerprint_path() {
  printf '%s/.deps-fingerprint\n' "${VENV_PATH}"
}

dependency_fingerprint() {
  "${PYTHON_BIN}" - "${ROOT_DIR}" "$(install_mode)" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
install_mode = sys.argv[2]
digest = hashlib.sha256()
digest.update(b"install-mode")
digest.update(b"\0")
digest.update(install_mode.encode())
digest.update(b"\0")
for name in ("pyproject.toml", "uv.lock"):
    path = root / name
    if not path.exists():
        continue
    digest.update(name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

deps_fingerprint_changed() {
  local fingerprint_file
  fingerprint_file="$(deps_fingerprint_path)"

  if [[ ! -f "${fingerprint_file}" ]]; then
    return 0
  fi

  local current_fingerprint
  current_fingerprint="$(dependency_fingerprint)"
  local installed_fingerprint
  installed_fingerprint="$(cat "${fingerprint_file}")"

  [[ "${current_fingerprint}" != "${installed_fingerprint}" ]]
}

record_deps_synced() {
  local fingerprint_file
  local tmp_file
  fingerprint_file="$(deps_fingerprint_path)"
  tmp_file="${fingerprint_file}.tmp.$$"

  dependency_fingerprint > "${tmp_file}"
  mv "${tmp_file}" "${fingerprint_file}"
  touch "$(deps_marker_path)"
}

uv_bin_path() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  if [[ -x "${HOME}/.local/bin/uv" ]]; then
    printf '%s\n' "${HOME}/.local/bin/uv"
    return 0
  fi
  return 1
}

ensure_pip() {
  if "${VENV_PATH}/bin/python" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  "${VENV_PATH}/bin/python" -m ensurepip --upgrade
}

install_mode() {
  local uv_bin
  uv_bin="$(uv_bin_path || true)"

  if [[ -n "${uv_bin}" && "${VENV_PATH}" == "${ROOT_DIR}/.venv" && -f "${ROOT_DIR}/uv.lock" ]]; then
    printf 'uv-frozen-extra-dev\n'
  else
    printf 'pip-editable-dev\n'
  fi
}

# Refuse to install this project into a venv another checkout owns.
#
# ensure_deps deliberately REPOINTS a stale editable install (#6912). That is
# correct from the checkout that owns the venv and is exactly the contamination
# this repo fixed when run from a worktree whose .venv is symlinked at the base
# venv: it would "repair" the shared venv by aiming it at the worktree.
#
# Routed through scripts/venv_guard.sh -- the single mutation-authorization
# owner -- which is pure shell precisely so it can be consulted here, before
# the package is importable.
VENV_DECISION=""

venv_mutation_outcome() {
  local guard="${ROOT_DIR}/scripts/venv_guard.sh"
  # Fail CLOSED. A missing or non-executable guard is not evidence of
  # ownership; treating it as such turned "guard absent" into "full project
  # sync", the exact mutation this exists to prevent.
  if [[ ! -x "${guard}" ]]; then
    return 3
  fi
  # VENV_PATH honours CC_VENV_PATH and may differ from ${ROOT_DIR}/.venv.
  # Guarding one path while mutating another guards nothing.
  # Capture the whole decision: the remedy travels with it, and every caller
  # passes --quiet, so a fix written only to the guard's stderr is unreachable.
  VENV_DECISION="$(cd "${ROOT_DIR}" && "${guard}" decide --quiet --venv "${VENV_PATH}" --operation install-project 2>/dev/null)"
  return $?
}

venv_decision_remedy() {
  printf '%s\n' "${VENV_DECISION}" | sed -n 's/^remedy=//p'
}

sync_deps() {
  local uv_bin
  local mode
  local guard_outcome=0
  uv_bin="$(uv_bin_path || true)"
  mode="$(install_mode)"

  venv_mutation_outcome || guard_outcome=$?

  # One validator, shared with Make and release prep. Each entry point used to
  # verify a different subset of the record, so a decision that disagreed with
  # the request was accepted by whichever caller happened not to check it.
  local guard_bin="${ROOT_DIR}/scripts/venv_guard.sh"
  if ! printf '%s\n' "${VENV_DECISION}" | "${guard_bin}" check \
        --venv "${VENV_PATH}" --operation install-project --exit "${guard_outcome}" \
        >/dev/null; then
    echo "ERROR: refusing to install this checkout's project into ${VENV_PATH}." >&2
    remedy="$(venv_decision_remedy)"
    [[ -n "${remedy}" ]] && echo "  To fix: ${remedy}" >&2
    return 1
  fi

  case "${guard_outcome}" in
    0) ;;
    1)
      echo "ERROR: ${VENV_PATH} is shared from another checkout." >&2
      echo "Refusing to install this checkout's project into it; that is what" >&2
      echo "repoints the shared editable install." >&2
      echo "  To fix: $(venv_decision_remedy)" >&2
      return 1
      ;;
    2)
      echo "ERROR: ${VENV_PATH} is a dangling symlink; cannot sync." >&2
      echo "  To fix: $(venv_decision_remedy)" >&2
      return 1
      ;;
    *)
      echo "ERROR: venv guard unavailable or returned ${guard_outcome}." >&2
      echo "Refusing to mutate ${VENV_PATH} without an authorization decision." >&2
      remedy="$(venv_decision_remedy)"
      [[ -n "${remedy}" ]] && echo "  To fix: ${remedy}" >&2
      return 1
      ;;
  esac

  if [[ "${mode}" == "uv-frozen-extra-dev" ]]; then
    echo "Syncing Python dependencies from ${ROOT_DIR} with uv..."
    # An inherited UV_PROJECT_ENVIRONMENT redirects `uv sync` away from
    # VENV_PATH. That leaves the stale environment detected by ensure_deps
    # untouched while uv successfully updates a different environment.
    (
      cd "${ROOT_DIR}"
      UV_PROJECT_ENVIRONMENT="${VENV_PATH}" "${uv_bin}" sync --frozen --extra dev
    )
  else
    echo "Syncing Python dependencies from ${ROOT_DIR} with pip..."
    ensure_pip
    (cd "${ROOT_DIR}" && "${VENV_PATH}/bin/python" -m pip install -e ".[dev]")
  fi
  verify_project_install
  record_deps_synced
}

installed_project_path() {
  # Isolated mode ignores PYTHONPATH (including inherited CC snapshots) and the
  # current directory while retaining this venv's site-packages. The probe must
  # inspect the installed editable, not an import-path override.
  "${VENV_PATH}/bin/python" \
    -I \
    -c "from pathlib import Path; import issue_orchestrator; print(Path(issue_orchestrator.__file__).resolve())" \
    2>/dev/null || true
}

project_root_path() {
  (cd "${ROOT_DIR}" && pwd -P)
}

project_install_is_current() {
  local installed_path="$1"
  local root_path
  [[ -n "${installed_path}" ]] || return 1
  root_path="$(project_root_path)"
  [[ "${installed_path}" == "${root_path}"/* ]]
}

verify_project_install() {
  local installed_path
  installed_path="$(installed_project_path)"
  if ! project_install_is_current "${installed_path}"; then
    echo "ERROR: Dependency sync did not install issue_orchestrator from ${ROOT_DIR}: ${installed_path:-not importable}" >&2
    return 1
  fi
}

ensure_deps() {
  # Check if installed AND pointing to this repo (not a stale worktree)
  local installed_path
  installed_path="$(installed_project_path)"

  if [[ -z "${installed_path}" ]]; then
    echo "Package not installed."
    sync_deps
  elif ! project_install_is_current "${installed_path}"; then
    echo "Stale install detected: ${installed_path}"
    sync_deps
  elif deps_fingerprint_changed; then
    echo "Dependency metadata changed since the last install."
    sync_deps
  fi
}

is_our_process() {
  # Check if a PID belongs to the orchestrator (python, uvicorn, or entry point)
  local pid="$1"
  local cmdline
  cmdline=$(ps -p "${pid}" -o args= 2>/dev/null || echo "")
  if [[ -z "${cmdline}" ]]; then
    return 1  # Process already exited
  fi
  # Match python, uvicorn, or our entry point binary/module
  if echo "${cmdline}" | grep -q -i -E 'python|uvicorn|issue.orchestrator|issue-orchestrator'; then
    return 0
  fi
  return 1
}

ensure_port_free() {
  local pids attempt

  # Only LISTEN sockets can block server bind. Established client connections
  # should not be treated as port conflicts.
  port_listener_pids() {
    lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
  }

  # Get listener PIDs on the port (there may be multiple - parent/child)
  pids=$(port_listener_pids)

  if [[ -z "${pids}" ]]; then
    return 0  # Port is free
  fi

  echo "Port ${PORT} is in use by PID(s): ${pids}"

  # Try graceful kill (SIGTERM) first
  for pid in ${pids}; do
    if is_our_process "${pid}"; then
      echo "Sending SIGTERM to old control center (PID ${pid})..."
      kill "${pid}" 2>/dev/null || true
    else
      echo "WARNING: PID ${pid} is not an orchestrator process, skipping"
    fi
  done

  # Wait for port to be freed (up to 5 seconds)
  for attempt in {1..10}; do
    sleep 0.5
    if [[ -z "$(port_listener_pids)" ]]; then
      echo "Port ${PORT} is now free"
      return 0
    fi
  done

  # SIGTERM didn't work, try SIGKILL
  echo "Graceful shutdown timed out, trying SIGKILL..."
  pids=$(port_listener_pids)
  local killed_any=false
  for pid in ${pids}; do
    if is_our_process "${pid}"; then
      echo "Sending SIGKILL to PID ${pid}..."
      kill -9 "${pid}" 2>/dev/null || true
      killed_any=true
    fi
  done

  # Final wait
  sleep 1
  if [[ -n "$(port_listener_pids)" ]]; then
    if [[ "${killed_any}" == "false" ]]; then
      echo "ERROR: Port ${PORT} is in use by non-orchestrator process(es)"
    else
      echo "ERROR: Could not free port ${PORT} even with SIGKILL"
    fi
    echo "Please check what's using it: lsof -i :${PORT}"
    exit 1
  fi

  echo "Port ${PORT} is now free (required SIGKILL)"
}

show_startup_info() {
  local commit_sha
  commit_sha=$(cd "${ROOT_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  echo "=== Starting Control Center ==="
  echo "  Repo: ${ROOT_DIR}"
  echo "  Port: ${PORT}"
  echo "  Commit: ${commit_sha}"
}

is_linked_worktree() {
  # In a linked worktree, --git-dir points to .git/worktrees/<name>
  # while --git-common-dir points to the shared base .git directory.
  local git_dir
  local git_common_dir
  git_dir=$(cd "${ROOT_DIR}" && git rev-parse --path-format=absolute --git-dir 2>/dev/null || echo "")
  git_common_dir=$(cd "${ROOT_DIR}" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "")

  [[ -n "${git_dir}" && -n "${git_common_dir}" && "${git_dir}" != "${git_common_dir}" ]]
}

main() {
  stop_all_orchestrators

  # Pull latest code only when running from the base repo on main/master.
  if is_linked_worktree; then
    echo "=== Skipping git pull (linked worktree detected) ==="
  else
    git_pull
  fi

  ensure_venv
  ensure_deps
  create_cc_snapshot
  ensure_port_free
  show_startup_info

  # Start control center entrypoint directly for deterministic startup.
  # IO_DEV disables static file caching so CSS/JS changes are visible immediately
  export IO_DEV=1
  export ISSUE_ORCHESTRATOR_CC_REPO_ROOT="${ROOT_DIR}"
  export ISSUE_ORCHESTRATOR_CC_COMMIT_SHA
  ISSUE_ORCHESTRATOR_CC_COMMIT_SHA=$(cd "${ROOT_DIR}" && git rev-parse HEAD 2>/dev/null || true)
  # Prepend the frozen snapshot to PYTHONPATH. Python consults PYTHONPATH
  # before site-packages, so every ``import issue_orchestrator`` — in the
  # CC and in every subprocess that inherits this env (``coding-done``,
  # agent tmux sessions, pre-push hooks, validate runners) — resolves to
  # the frozen copy rather than the editable install's mutable target.
  # This is the behaviour that makes the CC immune to base-repo branch
  # drift mid-run (see issue #5950).
  export PYTHONPATH="${SNAPSHOT_PYTHONPATH_ENTRY}${PYTHONPATH:+:${PYTHONPATH}}"
  # ``PYTHONPATH`` does the actual import-path work; ``ISSUE_ORCHESTRATOR_CC_SNAPSHOT``
  # is the observability companion — the CC logs it on startup and the
  # value is inspected by operators / tests to confirm the freeze landed.
  # Kept separate because a future change to ``PYTHONPATH`` composition
  # (extra prefixes, cache dirs) shouldn't pollute the observability
  # contract.
  export ISSUE_ORCHESTRATOR_CC_SNAPSHOT="${SNAPSHOT_PYTHONPATH_ENTRY}"
  exec "${VENV_PATH}/bin/python" -m issue_orchestrator.entrypoints.control_center --port "${PORT}" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
