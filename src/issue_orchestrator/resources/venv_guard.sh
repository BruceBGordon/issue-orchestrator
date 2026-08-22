#!/usr/bin/env bash
#
# THE mutation-authorization owner for a Python environment.
#
# One execution answers the whole question: may this checkout mutate this venv,
# and if so with exactly which arguments. Callers must never re-derive either
# half. Splitting the decision from its permitted arguments across two
# executions is what let a failed second call degrade into a plain, unrestricted
# `uv sync`.
#
# It is deliberately pure shell with no Python dependency: Control Center
# consults it before the package is importable. Python callers reach it through
# issue_orchestrator.infra.venv_mutation.VenvMutationAuthority, which resolves
# THIS file from the installed package rather than from the target checkout --
# an arbitrary target repository has no reason to carry it.
#
# WHY THIS EXISTS
# The orchestrator links a repo's venv into every worktree it creates
# (adapters/worktree/_worktree_runtime.py::_link_repo_venv_into_worktree).
# Installing a worktree's project through that link rewrites the shared venv's
# editable pointer, so imports resolve to whichever checkout last ran setup and
# dangle once it is removed.
#
# SUBCOMMANDS
#   decide   emit a decision record and exit with the outcome code
#   claim    bind an external venv to this checkout (writes the owner marker)
#
# OPERATIONS (--operation, default sync-dependencies)
#   sync-dependencies  update dependencies only; permitted for owned and shared
#   install-project    install THIS checkout's project; owned only
#   recreate           create or rebuild the environment; owned only
#
# The operation is part of the question. Asking only "who owns this?" leaves
# each caller to decide whether `shared` permits a dependency sync, a project
# install, or a full recreate -- and they answered differently, which is how a
# shared venv still got rebuilt by `uv venv`.
#
# OUTCOMES (exit codes)
#   0  owned      mutate freely, project install included
#   1  shared     another checkout owns it; dependency-only operations only
#   2  broken     dangling symlink; refuse
#   3  unclaimed  outside any checkout and not bound to this one; refuse
#  64  usage      bad arguments
#
# Callers must classify ALL outcomes exhaustively and FAIL CLOSED on anything
# unrecognised, including this script being missing or non-executable.
# Availability cannot be inferred from an exit code: under `set -e`, /bin/sh
# reports a missing command through `||` as status 1, which is indistinguishable
# from `shared`.

set -uo pipefail

OWNED=0
SHARED=1
BROKEN=2
UNCLAIMED=3
USAGE=64

OWNER_MARKER=".issue-orchestrator-venv-owner"

command="decide"
case "${1:-}" in
  decide|claim) command="$1"; shift ;;
esac

quiet=0
venv_path=""
checkout=""
operation="sync-dependencies"
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) quiet=1 ;;
    --venv) venv_path="${2:-}"; shift ;;
    --checkout) checkout="${2:-}"; shift ;;
    --operation) operation="${2:-}"; shift ;;
    *) echo "venv-guard: unknown argument: $1" >&2; exit "$USAGE" ;;
  esac
  shift
done

[ -n "$checkout" ] || checkout="$(pwd -P)"
checkout="$(cd "$checkout" 2>/dev/null && pwd -P)" || {
  echo "venv-guard: checkout does not exist" >&2; exit "$USAGE"
}
[ -n "$venv_path" ] || venv_path="$checkout/.venv"

case "$operation" in
  sync-dependencies|install-project|recreate) ;;
  *) echo "venv-guard: unknown operation: $operation" >&2; exit "$USAGE" ;;
esac

# Canonicalise the target. A relative path would be echoed back verbatim and
# then used to pin UV_PROJECT_ENVIRONMENT, which uv resolves against ITS cwd --
# so the pin would name a different environment than the one authorized.
case "$venv_path" in
  /*) ;;
  *) venv_path="$checkout/$venv_path" ;;
esac

# Dependency-only arguments. --no-install-project is the load-bearing flag: it
# updates dependencies without reinstalling the project, so the editable
# pointer is never rewritten. --inexact stops one checkout's sync removing
# packages another user of the same environment still needs.
ARGS_OWNED="--frozen --all-extras"
ARGS_SHARED="--frozen --all-extras --no-install-project --inexact"

# The command a caller should surface verbatim. Resolve the wrapper when it
# exists, because that is the path a human or agent already knows; fall back to
# this file otherwise.
self_path() {
  if [ -x "$checkout/scripts/venv_guard.sh" ]; then
    printf '%s' "$checkout/scripts/venv_guard.sh"
  else
    printf '%s' "$0"
  fi
}

# operation_allowed <outcome> -> 0 when this outcome permits $operation
operation_allowed() {
  case "$operation" in
    sync-dependencies) [ "$1" = "owned" ] || [ "$1" = "shared" ] ;;
    install-project|recreate) [ "$1" = "owned" ] ;;
    *) return 1 ;;
  esac
}

# emit <outcome> <args> <reason> [remedy]
#
# `remedy` is part of the decision, not decoration. Every caller passes
# --quiet, so anything written only to stderr is unreachable in practice: a
# refusal that does not carry its own fix reaches the operator as a bare
# "external and unclaimed" with nothing to act on.
emit() {
  # `claim` reports a decision as much as `decide` does -- a refused claim has
  # to name the holder, or the caller cannot tell why it lost.
  if [ "$command" = "decide" ] || [ "$command" = "claim" ]; then
    printf 'outcome=%s\n' "$1"
    printf 'sync_args=%s\n' "$2"
    printf 'venv=%s\n' "$venv_path"
    printf 'reason=%s\n' "$3"
    printf 'remedy=%s\n' "${4:-}"
    printf 'operation=%s\n' "$operation"
    if operation_allowed "$1"; then
      printf 'allowed=yes\n'
    else
      printf 'allowed=no\n'
    fi
  fi
}

note() { [ "$quiet" -eq 0 ] && printf 'venv-guard: %s\n' "$1" >&2 || true; }

# --- broken: a dangling link cannot be used, and creating over it writes into
# --- a dead path.
if [ -L "$venv_path" ] && [ ! -e "$venv_path" ]; then
  emit broken "" "dangling symlink -> $(readlink "$venv_path")" \
    "the checkout that owned it was deleted; remove the dead link and rebuild: rm $venv_path && make venv-fast"
  note "$venv_path is a DANGLING symlink -> $(readlink "$venv_path"). Remove it and rebuild."
  exit "$BROKEN"
fi

# --- absent inside this checkout: nothing to protect yet.
if [ ! -e "$venv_path" ]; then
  case "$venv_path" in
    "$checkout"/*) emit owned "$ARGS_OWNED" "absent inside this checkout"; exit "$OWNED" ;;
  esac
fi

resolved=""
if [ -e "$venv_path" ]; then
  resolved="$(cd "$venv_path" 2>/dev/null && pwd -P)" || resolved=""
fi
[ -n "$resolved" ] || resolved="$venv_path"

# --- inside this checkout: ours.
case "$resolved" in
  "$checkout"/*) emit owned "$ARGS_OWNED" "inside this checkout"; exit "$OWNED" ;;
esac

owner_dir="${resolved%/*}"

# --- owned by another checkout.
if [ -e "$owner_dir/pyproject.toml" ] || [ -e "$owner_dir/.git" ]; then
  emit shared "$ARGS_SHARED" "owned by checkout $owner_dir" \
    "dependencies are synced; to install THIS project run it where the venv lives: make -C $owner_dir install (or give this checkout its own: rm $venv_path && make venv-fast)"
  note "$venv_path is SHARED from $owner_dir; dependency-only operations only."
  exit "$SHARED"
fi

# --- external. "Not a checkout" proves nothing about exclusive use: two
# --- checkouts can point CC_VENV_PATH at the same environment and both would
# --- otherwise be told they own it. Require an explicit binding.
marker="$resolved/$OWNER_MARKER"
if [ "$command" = "claim" ]; then
  mkdir -p "$resolved" || { note "cannot create $resolved"; exit "$USAGE"; }
  # Atomic no-clobber create. Two checkouts claiming concurrently must not both
  # be told they own it: `set -C` makes the redirect fail if the marker already
  # exists, so exactly one creator wins and the loser is told who holds it.
  if (set -C; printf '%s\n' "$checkout" > "$marker") 2>/dev/null; then
    emit owned "$ARGS_OWNED" "claimed by this checkout"
    note "claimed $resolved for $checkout"
    exit "$OWNED"
  fi
  existing="$(head -n 1 "$marker" 2>/dev/null || true)"
  if [ "$existing" = "$checkout" ]; then
    # Re-claiming your own environment is idempotent, not a conflict.
    emit owned "$ARGS_OWNED" "already claimed by this checkout"
    exit "$OWNED"
  fi
  emit shared "$ARGS_SHARED" "already claimed by $existing" \
    "$existing holds this environment; it may be mutating it now. Use its checkout, or give this one its own venv. To take it over deliberately, remove $marker by hand first."
  note "refusing to steal a claim held by $existing"
  exit "$SHARED"
fi

if [ -f "$marker" ]; then
  claimed="$(head -n 1 "$marker" 2>/dev/null || true)"
  if [ "$claimed" = "$checkout" ]; then
    emit owned "$ARGS_OWNED" "claimed by this checkout"
    exit "$OWNED"
  fi
  emit shared "$ARGS_SHARED" "claimed by $claimed" \
    "$claimed claimed this environment; run from there, or point this checkout at its own venv"
  note "$venv_path is claimed by $claimed; dependency-only operations only."
  exit "$SHARED"
fi

emit unclaimed "" "external and unclaimed" \
  "this venv is outside every checkout, so nothing proves another one is not also using it. If this checkout is its only user, bind it once: $(self_path) claim --venv $venv_path -- otherwise point this checkout at its own venv: unset the override and run make venv-fast"
note "$venv_path is outside any checkout and not bound to one.
  Refusing to mutate it: nothing proves another checkout is not using it too.
  If this checkout is its only user, bind it once:
    $(self_path) claim --venv $venv_path
  Otherwise point this checkout at its own venv instead."
exit "$UNCLAIMED"
