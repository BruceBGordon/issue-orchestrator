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
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) quiet=1 ;;
    --venv) venv_path="${2:-}"; shift ;;
    --checkout) checkout="${2:-}"; shift ;;
    *) echo "venv-guard: unknown argument: $1" >&2; exit "$USAGE" ;;
  esac
  shift
done

[ -n "$checkout" ] || checkout="$(pwd -P)"
checkout="$(cd "$checkout" 2>/dev/null && pwd -P)" || {
  echo "venv-guard: checkout does not exist" >&2; exit "$USAGE"
}
[ -n "$venv_path" ] || venv_path="$checkout/.venv"

# Dependency-only arguments. --no-install-project is the load-bearing flag: it
# updates dependencies without reinstalling the project, so the editable
# pointer is never rewritten. --inexact stops one checkout's sync removing
# packages another user of the same environment still needs.
ARGS_OWNED="--frozen --all-extras"
ARGS_SHARED="--frozen --all-extras --no-install-project --inexact"

emit() {  # emit <outcome> <args> <reason>
  if [ "$command" = "decide" ]; then
    printf 'outcome=%s\n' "$1"
    printf 'sync_args=%s\n' "$2"
    printf 'venv=%s\n' "$venv_path"
    printf 'reason=%s\n' "$3"
  fi
}

note() { [ "$quiet" -eq 0 ] && printf 'venv-guard: %s\n' "$1" >&2 || true; }

# --- broken: a dangling link cannot be used, and creating over it writes into
# --- a dead path.
if [ -L "$venv_path" ] && [ ! -e "$venv_path" ]; then
  emit broken "" "dangling symlink -> $(readlink "$venv_path")"
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
  emit shared "$ARGS_SHARED" "owned by checkout $owner_dir"
  note "$venv_path is SHARED from $owner_dir; dependency-only operations only."
  exit "$SHARED"
fi

# --- external. "Not a checkout" proves nothing about exclusive use: two
# --- checkouts can point CC_VENV_PATH at the same environment and both would
# --- otherwise be told they own it. Require an explicit binding.
marker="$resolved/$OWNER_MARKER"
if [ "$command" = "claim" ]; then
  mkdir -p "$resolved" || { note "cannot create $resolved"; exit "$USAGE"; }
  printf '%s\n' "$checkout" > "$marker" || { note "cannot write $marker"; exit "$USAGE"; }
  emit owned "$ARGS_OWNED" "claimed by this checkout"
  note "claimed $resolved for $checkout"
  exit "$OWNED"
fi

if [ -f "$marker" ]; then
  claimed="$(head -n 1 "$marker" 2>/dev/null || true)"
  if [ "$claimed" = "$checkout" ]; then
    emit owned "$ARGS_OWNED" "claimed by this checkout"
    exit "$OWNED"
  fi
  emit shared "$ARGS_SHARED" "claimed by $claimed"
  note "$venv_path is claimed by $claimed; dependency-only operations only."
  exit "$SHARED"
fi

emit unclaimed "" "external and unclaimed"
note "$venv_path is outside any checkout and not bound to one.
  Refusing to mutate it: nothing proves another checkout is not using it too.
  Bind it explicitly first:
    $0 claim --venv $venv_path"
exit "$UNCLAIMED"
